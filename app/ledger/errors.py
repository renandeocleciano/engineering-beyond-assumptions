"""Domain errors for the laboratory ledger. HTTP mapping lives in the API layer."""


class LedgerError(Exception):
    pass


class AccountNotFound(LedgerError):
    pass


class TransferNotFound(LedgerError):
    pass


class AccountUnavailable(LedgerError):
    pass


class CurrencyMismatch(LedgerError):
    pass


class InsufficientFunds(LedgerError):
    pass


class IdempotencyKeyConflict(LedgerError):
    """Same scope and key with a different request fingerprint."""


class IdempotencyKeyRequired(LedgerError):
    pass
