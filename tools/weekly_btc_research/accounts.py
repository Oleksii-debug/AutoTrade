import json
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

PROTOCOL_PATH = Path(__file__).with_name("protocol.json")
PROTOCOL = "ee9562e1c00efd2e7bd6af4a1e468dbdcd78ac00c4f48d7ece0276a0bd425f1d"


def configuration():
    raw = PROTOCOL_PATH.read_bytes()
    if sha256(raw).hexdigest() != PROTOCOL:
        raise ValueError("Frozen weekly BTC protocol changed")
    return json.loads(raw, parse_float=Decimal)


@dataclass
class Account:
    cash: Decimal
    quantity: Decimal = Decimal(0)
    fees: Decimal = Decimal(0)

    def update(self, active, price, fee, slip, stake):
        if active and not self.quantity:
            spent = self.cash * stake
            notional = spent / (1 + fee)
            self.quantity = notional / (price * (1 + slip))
            self.cash -= spent
            self.fees += notional * fee
        elif not active and self.quantity:
            notional = self.quantity * price * (1 - slip)
            self.cash += notional * (1 - fee)
            self.fees += notional * fee
            self.quantity = Decimal(0)

    def liquidation(self, price, fee, slip):
        return self.cash + self.quantity * price * (1 - slip) * (1 - fee)
