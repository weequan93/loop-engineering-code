"""Atomic, replayable spend reservations owned by the controller projection.

The caller holds Store.writer and saves a reservation BEFORE dispatch. Ambiguous
requests retain their full reservation. Rates are explicit, frozen configuration.
"""

from copy import deepcopy

from reference.core import canonical_digest
from .contracts import ContractError


class BudgetError(ContractError):
    pass


def nonnegative(value) -> bool:
    return type(value) is int and 0 <= value <= 9007199254740991


def cost(input_tokens: int, output_tokens: int, pricing: dict | None) -> int | None:
    if pricing is None:
        return None
    # Count cached input at the full configured input ceiling; never assume a
    # discount. Ceiling each component also bounds billing rounding down to units.
    return ((input_tokens * pricing["input_microunits_per_million"] + 999999) // 1000000
            + (output_tokens * pricing["output_microunits_per_million"] + 999999) // 1000000)


class Ledger:
    def __init__(self, reservations: list[dict]):
        self.reservations = reservations

    def balance(self) -> dict:
        tokens, money, known_tokens, known_cost, currencies, unknown = 0, 0, 0, 0, set(), False
        token_bound_known, cost_bound_known = True, True
        for row in self.reservations:
            actual = row["actual"]
            quoted_tokens = row["input_tokens"] + row["max_output_tokens"]
            quoted_cost = cost(row["input_tokens"], row["max_output_tokens"], row["pricing"])
            if row["pricing"]:
                currencies.add(row["pricing"]["currency"])
            if row["status"] == "settled":
                used_tokens = actual["input_tokens"] + actual["output_tokens"]
                used_cost = cost(actual["input_tokens"], actual["output_tokens"], row["pricing"])
                tokens += used_tokens
                known_tokens += used_tokens
                if used_cost is not None:
                    money += used_cost; known_cost += used_cost
                else:
                    cost_bound_known = False
            else:
                tokens += quoted_tokens
                if quoted_cost is not None:
                    money += quoted_cost
                else:
                    cost_bound_known = False
                unknown = True
                if row["bound_verified"] is not True or row.get("violated_bound"):
                    token_bound_known = False
                if row.get("violated_bound"):
                    cost_bound_known = False
        if len(currencies) > 1:
            raise BudgetError("Budget ledger contains mixed currencies")
        return {"tokens_upper": tokens if token_bound_known else None,
                "cost_upper_microunits": money if cost_bound_known else None,
                "known_tokens": known_tokens, "known_cost_microunits": known_cost,
                "actual_tokens": known_tokens if not unknown else None,
                "actual_cost_microunits": known_cost if not unknown and cost_bound_known else None,
                "currency": next(iter(currencies), None), "unknown_requests": sum(r["status"] != "settled" for r in self.reservations),
                "violated_bound": any(r.get("violated_bound", False) for r in self.reservations)}

    def reserve(self, request_id: str, quote: dict, limits: dict, *, final_tokens: int = 0,
                final_cost: int = 0) -> dict:
        if any(r["request_id"] == request_id for r in self.reservations):
            raise BudgetError("Request ID was already reserved; reconcile rather than replay")
        if (set(quote) != {"input_tokens", "max_output_tokens", "bound_verified", "pricing", "request_digest"}
                or not nonnegative(quote["input_tokens"]) or not nonnegative(quote["max_output_tokens"])
                or type(quote["bound_verified"]) is not bool
                or not nonnegative(final_tokens) or not nonnegative(final_cost)):
            raise BudgetError("Invalid response reservation")
        pricing = quote["pricing"]
        if pricing is not None:
            from .native_contracts import validate_native
            validate_native("pricing", pricing)
            if pricing["currency"] != limits["cost_currency"]:
                raise BudgetError("Configured pricing currency differs from task budget")
        balance = self.balance()
        if balance["violated_bound"]:
            raise BudgetError("Provider violated a reserved bound; reconcile before any new dispatch")
        amount = quote["input_tokens"] + quote["max_output_tokens"]
        charge = cost(quote["input_tokens"], quote["max_output_tokens"], pricing)
        if (not nonnegative(amount) or not nonnegative(balance["known_tokens"] + amount + final_tokens)
                or balance["tokens_upper"] is not None and not nonnegative(balance["tokens_upper"] + amount + final_tokens)
                or charge is not None and (not nonnegative(charge)
                    or not nonnegative(balance["known_cost_microunits"] + charge + final_cost)
                    or balance["cost_upper_microunits"] is not None
                    and not nonnegative(balance["cost_upper_microunits"] + charge + final_cost))):
            raise BudgetError("Reservation accounting exceeds the portable integer range")
        if limits["max_tokens"] is not None:
            if quote["bound_verified"] is not True or balance["tokens_upper"] is None:
                raise BudgetError("Hard token cap requires a verified input/output bound and complete reservations")
            if balance["tokens_upper"] + amount + final_tokens > limits["max_tokens"]:
                raise BudgetError("Insufficient token budget after final verification/review reserve")
        if limits["max_cost_microunits"] is not None:
            if (quote["bound_verified"] is not True or charge is None or balance["cost_upper_microunits"] is None
                    or (balance["currency"] is not None and balance["currency"] != limits["cost_currency"])):
                raise BudgetError("Hard cost cap requires configured pricing and conservative spend bounds")
            if balance["cost_upper_microunits"] + charge + final_cost > limits["max_cost_microunits"]:
                raise BudgetError("Insufficient cost budget after final verification/review reserve")
        row = dict(deepcopy(quote), request_id=request_id, status="held", actual=None, violated_bound=False)
        from .native_contracts import validate_native
        validate_native("reservation", row)
        self.reservations.append(row)
        return row

    def settle(self, request_id: str, usage: dict | None) -> dict:
        row = next((r for r in self.reservations if r["request_id"] == request_id), None)
        if row is None:
            raise BudgetError("Usage has no admitted reservation")
        if row["status"] == "settled":
            if row["actual"] != usage:
                raise BudgetError("Conflicting duplicate usage report")
            return row
        if usage is None or set(usage) != {"input_tokens", "output_tokens"} or any(not nonnegative(v) for v in usage.values()):
            row.update(status="unknown", actual=None)
        else:
            balance = self.balance()
            total = usage["input_tokens"] + usage["output_tokens"]
            charge = cost(usage["input_tokens"], usage["output_tokens"], row["pricing"])
            if (not nonnegative(balance["known_tokens"] + total)
                    or charge is not None and not nonnegative(balance["known_cost_microunits"] + charge)):
                row.update(status="unknown", actual=None, violated_bound=True)
                return row
            row.update(status="settled", actual=deepcopy(usage))
            row["violated_bound"] = (row["bound_verified"] and (usage["input_tokens"] > row["input_tokens"]
                                      or usage["output_tokens"] > row["max_output_tokens"]))
        from .native_contracts import validate_native
        validate_native("reservation", row)
        return row

    def digest(self) -> str:
        return canonical_digest(self.reservations)
