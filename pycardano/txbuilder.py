from __future__ import annotations

from typing import List, Optional, Union

from pycardano.address import Address
from pycardano.backend.base import ChainContext
from pycardano.coinselection import LargestFirstSelector, RandomImproveMultiAsset, UTxOSelector
from pycardano.exception import UTxOSelectionException
from pycardano.hash import VerificationKeyHash, VERIFICATION_KEY_HASH_SIZE
from pycardano.key import VerificationKey
from pycardano.logging import logger
from pycardano.transaction import Value, Transaction, TransactionBody, TransactionOutput, UTxO
from pycardano.utils import max_tx_fee
from pycardano.witness import TransactionWitnessSet, VerificationKeyWitness

FAKE_VKEY = VerificationKey.from_primitive(bytes(VERIFICATION_KEY_HASH_SIZE))

# Ed25519 signature of a 32-bytes message (TX hash) will have length of 64
FAKE_TX_SIGNATURE = bytes(64)


class TransactionBuilder:
    """A class builder that makes it easy to build a transaction.

    Args:
        context (ChainContext): A chain context.
        utxo_selectors (Optional[List[UTxOSelector]]): A list of UTxOSelectors that will select input UTxOs.
    """

    def __init__(self, context: ChainContext, utxo_selectors: Optional[List[UTxOSelector]] = None):
        self.context = context
        self._inputs = []
        self._input_addresses = []
        self._outputs = []
        self._fee = 0
        self._ttl = None
        self._validity_start = None

        if utxo_selectors:
            self.utxo_selectors = utxo_selectors
        else:
            self.utxo_selectors = [RandomImproveMultiAsset(), LargestFirstSelector()]

    def add_input(self, utxo: UTxO) -> TransactionBuilder:
        self.inputs.append(utxo)
        return self

    def add_input_address(self, address: Union[Address, str]) -> TransactionBuilder:
        self.input_addresses.append(address)
        return self

    def add_output(self, tx_out: TransactionOutput):
        self.outputs.append(tx_out)
        return self

    @property
    def inputs(self) -> List[UTxO]:
        return self._inputs

    @property
    def input_addresses(self) -> List[Union[Address, str]]:
        return self._input_addresses

    @property
    def outputs(self) -> List[TransactionOutput]:
        return self._outputs

    @property
    def fee(self) -> int:
        return self._fee

    @fee.setter
    def fee(self, fee: int):
        self._fee = fee

    @property
    def ttl(self) -> int:
        return self._ttl

    @ttl.setter
    def ttl(self, ttl: int):
        self._ttl = ttl

    def set_ttl_by_delta(self, delta: int) -> TransactionBuilder:
        """Set time to live by number of seconds from now.

        Args:
            delta (int): Number of seconds (from now) after which the transaction will become invalid.

        Returns:
            TransactionBuild: Current transaction build.
        """
        delta_slots = delta // self.context.genesis_param.slot_length
        self.ttl = self.context.slot + delta_slots
        return self

    @property
    def validity_start(self):
        return self._validity_start

    @validity_start.setter
    def validity_start(self, validity_start: int):
        self._validity_start = validity_start

    def _add_change_and_fee(self, change_address: Address) -> TransactionBuilder:
        self.fee = max_tx_fee(self.context)
        requested = Value(self.fee)
        for o in self.outputs:
            requested += o.amount

        provided = Value()
        for i in self.inputs:
            provided += i.output.amount

        change = provided - requested

        # Remove any asset that has 0 quantity
        if change.multi_asset:
            change.multi_asset = change.multi_asset.filter(lambda p, n, v: v > 0)

        # If we end up with no multi asset, simply use coin value as change
        if not change.multi_asset:
            change = change.coin

        self.outputs.append(TransactionOutput(change_address, change))
        return self

    def _input_vkey_hashes(self) -> List[VerificationKeyHash]:
        results = set()
        for i in self.inputs:
            if isinstance(i.output.address.payment_part, VerificationKeyHash):
                results.add(i.output.address.payment_part)
        return list(results)

    def _build_tx_body(self) -> TransactionBody:
        tx_body = TransactionBody([i.input for i in self.inputs],
                                  self.outputs,
                                  fee=self.fee,
                                  ttl=self.ttl,
                                  validity_start=self.validity_start
                                  )
        return tx_body

    def _build_fake_vkey_witnesses(self) -> List[VerificationKeyWitness]:
        vkey_hashes = self._input_vkey_hashes()
        return [VerificationKeyWitness(FAKE_VKEY, FAKE_TX_SIGNATURE) for _ in vkey_hashes]

    def _build_fake_witness_set(self) -> TransactionWitnessSet:
        return TransactionWitnessSet(vkey_witnesses=self._build_fake_vkey_witnesses())

    def _build_full_fake_tx(self) -> Transaction:
        tx_body = self._build_tx_body()
        witness = self._build_fake_witness_set()
        return Transaction(tx_body, witness, True)

    def build(self, change_address: Optional[Address] = None) -> TransactionBody:
        """Build a transaction body from all constraints set through the builder.

        Args:
            change_address (Optional[Address]): Address to which changes will be returned. If not provided, the
                transaction body will likely be unbalanced (sum of inputs is greater than the sum of outputs).

        Returns:
            A transaction body.
        """
        selected_utxos = []
        selected_amount = Value()
        for i in self.inputs:
            selected_utxos.append(i)
            selected_amount += i.output.amount

        requested_amount = Value()
        for o in self.outputs:
            requested_amount += o.amount

        # Trim off assets that are not requested because they will be returned as changes eventually.
        trimmed_selected_amount = Value(selected_amount.coin,
                                        selected_amount.multi_asset.filter(
                                            lambda p, n, v: p in requested_amount.multi_asset and n in
                                            requested_amount.multi_asset[p]))

        unfulfilled_amount = requested_amount - trimmed_selected_amount
        unfulfilled_amount.coin = max(0, unfulfilled_amount.coin)

        if Value() < unfulfilled_amount:
            additional_utxo_pool = []
            for address in self.input_addresses:
                for utxo in self.context.utxos(str(address)):
                    if utxo not in selected_utxos:
                        additional_utxo_pool.append(utxo)

            for i, selector in enumerate(self.utxo_selectors):
                try:
                    selected, _ = selector.select(additional_utxo_pool,
                                                  [TransactionOutput(None, unfulfilled_amount)],
                                                  self.context)
                    for s in selected:
                        selected_amount += s.output.amount
                        selected_utxos.append(s)

                    break

                except UTxOSelectionException as e:
                    if i < len(self.utxo_selectors) - 1:
                        logger.info(e)
                        logger.info(f"{selector} failed. Trying next selector.")
                    else:
                        raise UTxOSelectionException("All UTxO selectors failed.")

        selected_utxos.sort(key=lambda utxo: (str(utxo.input.transaction_id), utxo.input.index))

        self.inputs[:] = selected_utxos[:]

        self._add_change_and_fee(change_address)

        tx_body = self._build_tx_body()

        return tx_body