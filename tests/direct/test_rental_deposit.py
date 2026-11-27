"""Direct-mode tests for RentalDepositCourt (genlayer-test 0.30.1).

Toolchain notes — read before touching the harness (see also
``tests/direct/conftest.py``):

- genlayer-test 0.30.1 targets the GenVM v0.3 SDK layout; the v0.6 std
  moved modules under ``genlayer.py.*``. ``conftest.py`` rebinds the
  loader's helpers and its contract allocator to the v0.6 layout.
- The v0.6 std snapshots ``gl.message`` once, at contract import time,
  and the loader only injects the message then. ``_sync_message()``
  rebuilds that snapshot from the VM before every call, so sender /
  value / warp changes made by a test are actually seen by the contract.
- The loader never captures the ``run_nondet_unsafe`` validator in this
  version (its patch targets ``genlayer.vm``, which does not exist in the
  v0.6 std, and only covers ``run_nondet`` anyway).
  ``_install_nondet_shim()`` aliases ``genlayer.vm`` to
  ``genlayer.gl.vm`` and replaces ``run_nondet_unsafe`` with a direct
  executor that records the validator for ``vm.run_validator()``.
- ``emit_transfer`` is a no-op in direct mode (the mock has no handler
  for the transfer message) and a payable ``value`` is not deducted from
  the sender. These tests therefore assert the contract's own payout
  accounting (``landlord_paid`` / ``tenant_paid`` / ``status`` /
  ``outcome``), not balances. Real value movement is only exercised on a
  live network / GenLayer Studio.
- Always run pytest with stdin from ``/dev/null``: the std reads the
  message context from fd 0 at import time, and a bare ``pytest`` run
  inherits a pipe that blocks forever on the first contract import.
"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

AMOUNT = 10 * 10**18  # 10 GEN deposit, in atto units
CLAIM = 4 * 10**18  # 4 GEN claimed by the landlord at checkout
T0 = "2026-11-27T00:00:00Z"
T0_EPOCH = int(datetime(2026, 11, 27, tzinfo=timezone.utc).timestamp())
DAY = 86400

LANDLORD_EVIDENCE = "Checkout photos: broken window pane, stained carpet, invoice for repairs."
TENANT_CLAIM = "Damage was pre-existing: window was cracked at move-in, noted in the handover report."
TENANT_EVIDENCE = "Move-in handover report with photos showing the cracked window."
EVIDENCE_URL = "https://example.com/checkout-proof"
EVIDENCE_PAGE = "Checkout report: window damage documented, repair invoice attached."


# --------------------------------------------------------------------------
# harness helpers
# --------------------------------------------------------------------------


def _sync_message(vm):
    """Rebuild the v0.6 std's ``gl.message`` snapshot from the VM state.

    The std builds ``gl.message`` once at import from the dict decoded
    off fd 0; later ``vm.sender`` / ``vm.value`` / ``vm.warp`` changes
    are invisible to the contract unless this runs first.
    """
    import genlayer.gl as gl_mod
    import genlayer._internal.msg as msg_mod
    from genlayer.py.types import Address, u256

    raw = vm.get_message_raw()

    def _addr(a):
        return a if isinstance(a, Address) else Address(a)

    message_raw = msg_mod.message_raw
    message_raw.clear()
    message_raw.update(
        {
            "contract_address": _addr(raw["contract_address"]),
            "sender_address": _addr(raw["sender_address"]),
            "origin_address": _addr(raw["origin_address"]),
            "stack": raw["stack"],
            "value": u256(raw["value"]),
            "datetime": raw["datetime"],
            "is_init": raw["is_init"],
            "chain_id": u256(raw["chain_id"]),
            "create2": raw["create2"],
            "entry_kind": raw["entry_kind"],
            "entry_data": raw["entry_data"],
            "entry_stage_data": raw["entry_stage_data"],
        }
    )
    gl_mod.message = gl_mod.MessageType(
        contract_address=message_raw["contract_address"],
        sender_address=message_raw["sender_address"],
        origin_address=message_raw["origin_address"],
        value=u256(message_raw["value"]),
        chain_id=u256(message_raw["chain_id"]),
    )


def _install_nondet_shim(vm):
    """Make ``run_nondet_unsafe`` testable in direct mode.

    Aliases ``genlayer.vm`` (missing in the v0.6 std) to
    ``genlayer.gl.vm`` so ``vm.run_validator()`` can import it, and
    replaces ``run_nondet_unsafe`` with a direct executor that runs the
    leader immediately and records ``(result, leader, validator)`` for
    ``vm.run_validator()``.
    """
    import sys

    import genlayer.gl.vm as gl_vm
    from gltest.direct import wasi_mock

    sys.modules.setdefault("genlayer.vm", gl_vm)
    if getattr(gl_vm, "_direct_test_patched", False):
        return

    def _direct_run_nondet_unsafe(leader_fn, validator_fn, /):
        _vm = wasi_mock.get_vm()
        _vm._in_nondet = True
        try:
            result = leader_fn()
        finally:
            _vm._in_nondet = False
        _vm._captured_validators.append((result, leader_fn, validator_fn))
        return result

    gl_vm.run_nondet_unsafe = _direct_run_nondet_unsafe
    gl_vm._direct_test_patched = True


def _hex(addr) -> str:
    """Canonical checksummed-hex string for a test address."""
    from genlayer.py.types import Address

    return Address(addr).as_hex if not isinstance(addr, Address) else addr.as_hex


@pytest.fixture
def env(direct_vm, direct_deploy, direct_alice, direct_bob):
    contract = direct_deploy("contracts/rental_deposit.py")
    _install_nondet_shim(direct_vm)
    direct_vm.warp(T0)
    _sync_message(direct_vm)
    return SimpleNamespace(
        vm=direct_vm, contract=contract, tenant=direct_alice, landlord=direct_bob
    )


def _as(env, addr):
    """Act as ``addr`` for the next call(s)."""
    env.vm.sender = addr
    _sync_message(env.vm)


def _warp(env, iso):
    env.vm.warp(iso)
    _sync_message(env.vm)


def _create(env, amount=AMOUNT, checkout_days=30, dispute_days=7, landlord=None):
    env.vm.deal(env.tenant, 100 * AMOUNT)
    _as(env, env.tenant)
    env.vm.value = amount
    _sync_message(env.vm)
    lease_id = env.contract.create_lease(
        landlord if landlord is not None else env.landlord,
        "Apartment 4B",
        "One-bedroom apartment, 12-month lease.",
        checkout_days,
        dispute_days,
    )
    env.vm.value = 0
    _sync_message(env.vm)
    return lease_id


def _record(env, lease_id):
    return json.loads(env.contract.get_lease(lease_id))


def _claim(env, lease_id, claim_amount=CLAIM, evidence=LANDLORD_EVIDENCE, url=""):
    _as(env, env.landlord)
    env.contract.claim_checkout(lease_id, claim_amount, evidence, url)


def _confirm(env, lease_id):
    _as(env, env.tenant)
    env.contract.confirm_checkout(lease_id)


def _dispute(env, lease_id, claim=TENANT_CLAIM, evidence=TENANT_EVIDENCE):
    _as(env, env.tenant)
    env.contract.open_dispute(lease_id, claim, evidence)


def _disputed(env, with_url=False):
    lease_id = _create(env)
    _claim(env, lease_id, url=EVIDENCE_URL if with_url else "")
    _dispute(env, lease_id)
    return lease_id


def _mock_verdict(vm, verdict, landlord_bps=0, confidence=80, reasoning="Judged."):
    vm.mock_llm(
        "adjudicator",
        json.dumps(
            {
                "verdict": verdict,
                "landlord_bps": landlord_bps,
                "confidence": confidence,
                "reasoning": reasoning,
            }
        ),
    )


def _mock_page(vm, body=EVIDENCE_PAGE):
    vm.mock_web("example.com", {"body": body})


def _adjudicate(env, lease_id):
    _as(env, env.tenant)
    env.contract.adjudicate(lease_id)


# --------------------------------------------------------------------------
# creation
# --------------------------------------------------------------------------


def test_create_and_read_fields(env):
    lease_id = _create(env)
    assert lease_id == 0
    rec = _record(env, 0)
    assert rec["tenant"] == _hex(env.tenant)
    assert rec["landlord"] == _hex(env.landlord)
    assert rec["amount"] == AMOUNT
    assert rec["title"] == "Apartment 4B"
    assert rec["status"] == "FUNDED"
    assert rec["created_at"] == T0_EPOCH
    assert rec["checkout_deadline"] == T0_EPOCH + 30 * DAY
    assert rec["dispute_deadline"] == T0_EPOCH + 37 * DAY
    assert rec["claim_amount"] == 0
    assert rec["landlord_paid"] == 0
    assert rec["tenant_paid"] == 0


def test_total_leases_counts(env):
    _create(env)
    _create(env)
    assert env.contract.total_leases() == 2


def test_create_rejects_zero_value(env):
    with env.vm.expect_revert("Deposit amount must be positive"):
        _create(env, amount=0)


def test_create_rejects_same_tenant_and_landlord(env):
    with env.vm.expect_revert("Tenant and landlord must differ"):
        _create(env, landlord=env.tenant)


def test_create_rejects_zero_address_landlord(env):
    zero = bytes(20)
    with env.vm.expect_revert("Landlord address must be set"):
        _create(env, landlord=zero)


@pytest.mark.parametrize("title", ["", "x" * 201])
def test_create_rejects_bad_title(env, title):
    env.vm.deal(env.tenant, 100 * AMOUNT)
    _as(env, env.tenant)
    env.vm.value = AMOUNT
    _sync_message(env.vm)
    with env.vm.expect_revert("Title must be"):
        env.contract.create_lease(env.landlord, title, "desc", 30, 7)


def test_create_rejects_empty_description(env):
    env.vm.deal(env.tenant, 100 * AMOUNT)
    _as(env, env.tenant)
    env.vm.value = AMOUNT
    _sync_message(env.vm)
    with env.vm.expect_revert("Description must be"):
        env.contract.create_lease(env.landlord, "title", "", 30, 7)


@pytest.mark.parametrize("checkout_days,dispute_days", [(0, 7), (181, 7), (30, 0), (30, 31)])
def test_create_rejects_bad_windows(env, checkout_days, dispute_days):
    with env.vm.expect_revert("window must be"):
        _create(env, checkout_days=checkout_days, dispute_days=dispute_days)


def test_unknown_lease_id_rejected(env):
    with env.vm.expect_revert("Unknown lease id"):
        env.contract.get_lease(99)


# --------------------------------------------------------------------------
# happy path + role / state guards
# --------------------------------------------------------------------------


def test_happy_path_confirm_splits_deposit_by_claim(env):
    lease_id = _create(env)
    _claim(env, lease_id)
    _confirm(env, lease_id)
    rec = _record(env, lease_id)
    assert rec["status"] == "CONFIRMED"
    assert rec["outcome"] == "release"
    assert rec["landlord_bps"] == 4000  # 4 of 10 GEN claimed
    assert rec["landlord_paid"] == CLAIM
    assert rec["tenant_paid"] == AMOUNT - CLAIM


def test_only_landlord_can_claim_checkout(env):
    lease_id = _create(env)
    _as(env, env.tenant)
    with env.vm.expect_revert("Only the landlord can do this"):
        env.contract.claim_checkout(lease_id, CLAIM, LANDLORD_EVIDENCE, "")


def test_only_tenant_can_confirm(env):
    lease_id = _create(env)
    _claim(env, lease_id)
    _as(env, env.landlord)
    with env.vm.expect_revert("Only the tenant can do this"):
        env.contract.confirm_checkout(lease_id)


def test_confirm_requires_claimed_state(env):
    lease_id = _create(env)
    _as(env, env.tenant)
    with env.vm.expect_revert("Only a claimed checkout can be confirmed"):
        env.contract.confirm_checkout(lease_id)


def test_claim_checkout_twice_rejected(env):
    lease_id = _create(env)
    _claim(env, lease_id)
    _as(env, env.landlord)
    with env.vm.expect_revert("Only a funded lease can be claimed at checkout"):
        env.contract.claim_checkout(lease_id, CLAIM, LANDLORD_EVIDENCE, "")


def test_claim_rejects_bad_amount(env):
    lease_id = _create(env)
    _as(env, env.landlord)
    with env.vm.expect_revert("Claim amount must be positive"):
        env.contract.claim_checkout(lease_id, 0, LANDLORD_EVIDENCE, "")
    with env.vm.expect_revert("Claim amount must be positive"):
        env.contract.claim_checkout(lease_id, AMOUNT + 1, LANDLORD_EVIDENCE, "")


def test_claim_rejects_empty_evidence_and_bad_url(env):
    lease_id = _create(env)
    _as(env, env.landlord)
    with env.vm.expect_revert("Claim evidence must be"):
        env.contract.claim_checkout(lease_id, CLAIM, "", "")
    with env.vm.expect_revert("Evidence URL must be an http(s) URL"):
        env.contract.claim_checkout(lease_id, CLAIM, LANDLORD_EVIDENCE, "ftp://example.com/x")


def test_dispute_requires_claimed_state(env):
    lease_id = _create(env)
    _as(env, env.tenant)
    with env.vm.expect_revert("Only a claimed checkout can be disputed"):
        env.contract.open_dispute(lease_id, TENANT_CLAIM, TENANT_EVIDENCE)


# --------------------------------------------------------------------------
# timeouts
# --------------------------------------------------------------------------


def test_timeout_refund_after_checkout_deadline(env):
    lease_id = _create(env)
    _warp(env, "2026-12-28T00:00:01Z")  # past 2026-12-27 checkout deadline
    _as(env, env.tenant)
    env.contract.claim_timeout_refund(lease_id)
    rec = _record(env, lease_id)
    assert rec["status"] == "REFUNDED"
    assert rec["outcome"] == "refund"
    assert rec["tenant_paid"] == AMOUNT
    assert rec["landlord_paid"] == 0


def test_timeout_refund_before_deadline_rejected(env):
    lease_id = _create(env)
    _as(env, env.tenant)
    with env.vm.expect_revert("Checkout deadline has not passed yet"):
        env.contract.claim_timeout_refund(lease_id)


def test_refunded_lease_is_final(env):
    lease_id = _create(env)
    _warp(env, "2026-12-28T00:00:01Z")
    _as(env, env.tenant)
    env.contract.claim_timeout_refund(lease_id)
    with env.vm.expect_revert("Only a funded, unclaimed lease can be refunded"):
        env.contract.claim_timeout_refund(lease_id)
    _as(env, env.landlord)
    with env.vm.expect_revert("Only a funded lease can be claimed at checkout"):
        env.contract.claim_checkout(lease_id, CLAIM, LANDLORD_EVIDENCE, "")


def test_silent_tenant_checkout_after_dispute_window(env):
    lease_id = _create(env)
    _claim(env, lease_id)
    _warp(env, "2027-01-04T00:00:01Z")  # past 2027-01-03 dispute deadline
    _as(env, env.landlord)
    env.contract.claim_checkout_after_window(lease_id)
    rec = _record(env, lease_id)
    assert rec["status"] == "CONFIRMED"
    assert rec["outcome"] == "release"
    assert rec["landlord_paid"] == CLAIM
    assert rec["tenant_paid"] == AMOUNT - CLAIM


def test_collect_before_window_and_confirm_after_window_rejected(env):
    lease_id = _create(env)
    _claim(env, lease_id)
    _as(env, env.landlord)
    with env.vm.expect_revert("Dispute window has not closed yet"):
        env.contract.claim_checkout_after_window(lease_id)
    _warp(env, "2027-01-04T00:00:01Z")
    _as(env, env.tenant)
    with env.vm.expect_revert("Dispute window has closed"):
        env.contract.confirm_checkout(lease_id)


# --------------------------------------------------------------------------
# disputes
# --------------------------------------------------------------------------


def test_dispute_flow_and_second_dispute_rejected(env):
    lease_id = _disputed(env)
    rec = _record(env, lease_id)
    assert rec["status"] == "DISPUTED"
    assert rec["disputer"] == "tenant"
    assert rec["dispute_claim"] == TENANT_CLAIM
    _as(env, env.tenant)
    with env.vm.expect_revert("Only a claimed checkout can be disputed"):
        env.contract.open_dispute(lease_id, "again", "again")


def test_only_tenant_can_dispute(env, direct_charlie):
    lease_id = _create(env)
    _claim(env, lease_id)
    _as(env, direct_charlie)
    with env.vm.expect_revert("Only the tenant can do this"):
        env.contract.open_dispute(lease_id, TENANT_CLAIM, TENANT_EVIDENCE)


def test_dispute_rejects_empty_claim_or_evidence(env):
    lease_id = _create(env)
    _claim(env, lease_id)
    _as(env, env.tenant)
    with env.vm.expect_revert("Dispute claim must be"):
        env.contract.open_dispute(lease_id, "", TENANT_EVIDENCE)
    with env.vm.expect_revert("Dispute evidence must be"):
        env.contract.open_dispute(lease_id, TENANT_CLAIM, "")


# --------------------------------------------------------------------------
# adjudication
# --------------------------------------------------------------------------


def test_adjudicate_release_keeps_full_claim(env):
    lease_id = _disputed(env)
    _mock_verdict(env.vm, "release", landlord_bps=4000, confidence=90)
    _adjudicate(env, lease_id)
    rec = _record(env, lease_id)
    assert rec["status"] == "ADJUDICATED"
    assert rec["outcome"] == "release"
    assert rec["landlord_bps"] == 4000
    assert rec["landlord_paid"] == CLAIM
    assert rec["tenant_paid"] == AMOUNT - CLAIM
    assert rec["confidence"] == 90
    assert env.vm.run_validator() is True


def test_adjudicate_refund(env):
    lease_id = _disputed(env)
    _mock_verdict(env.vm, "refund", landlord_bps=0, confidence=85)
    _adjudicate(env, lease_id)
    rec = _record(env, lease_id)
    assert rec["status"] == "ADJUDICATED"
    assert rec["outcome"] == "refund"
    assert rec["landlord_paid"] == 0
    assert rec["tenant_paid"] == AMOUNT
    assert env.vm.run_validator() is True


def test_adjudicate_split(env):
    lease_id = _disputed(env)
    _mock_verdict(env.vm, "split", landlord_bps=2000, confidence=70)
    _adjudicate(env, lease_id)
    rec = _record(env, lease_id)
    assert rec["status"] == "ADJUDICATED"
    assert rec["outcome"] == "split"
    assert rec["landlord_bps"] == 2000
    assert rec["landlord_paid"] == 2 * 10**18
    assert rec["tenant_paid"] == 8 * 10**18
    assert env.vm.run_validator() is True


def test_adjudicate_with_evidence_url_uses_fetched_page(env):
    lease_id = _disputed(env, with_url=True)
    _mock_page(env.vm)
    _mock_verdict(env.vm, "release", landlord_bps=4000, confidence=95)
    _adjudicate(env, lease_id)
    rec = _record(env, lease_id)
    assert rec["outcome"] == "release"
    assert rec["landlord_paid"] == CLAIM


def test_adjudicate_tolerates_dead_evidence_url(env):
    # No web mock registered: the fetch fails, the text evidence alone
    # is still judged and adjudication must not block.
    lease_id = _disputed(env, with_url=True)
    _mock_verdict(env.vm, "release", landlord_bps=4000, confidence=60)
    _adjudicate(env, lease_id)
    rec = _record(env, lease_id)
    assert rec["status"] == "ADJUDICATED"
    assert rec["outcome"] == "release"


def test_adjudicate_rejects_unparsable_llm(env):
    lease_id = _disputed(env)
    env.vm.mock_llm("adjudicator", "not json at all")
    with env.vm.expect_revert("[LLM_ERROR]"):
        _adjudicate(env, lease_id)


def test_adjudicate_requires_disputed_state(env):
    lease_id = _create(env)
    _mock_verdict(env.vm, "release", landlord_bps=4000)
    with env.vm.expect_revert("Only a disputed lease can be adjudicated"):
        _adjudicate(env, lease_id)
    _claim(env, lease_id)
    _dispute(env, lease_id)
    _adjudicate(env, lease_id)
    with env.vm.expect_revert("Only a disputed lease can be adjudicated"):
        _adjudicate(env, lease_id)


def test_validator_disagrees_when_llm_changes(env):
    lease_id = _disputed(env)
    _mock_verdict(env.vm, "release", landlord_bps=4000, confidence=90)
    _adjudicate(env, lease_id)
    # The validator re-runs the adjudication against the CURRENT mocks:
    # with a different verdict now, it must disagree.
    env.vm.clear_mocks()
    _mock_verdict(env.vm, "refund", landlord_bps=0, confidence=90)
    assert env.vm.run_validator() is False


def test_validator_rejects_leader_error(env):
    import genlayer.gl.vm as gl_vm

    lease_id = _disputed(env)
    _mock_verdict(env.vm, "release", landlord_bps=4000, confidence=90)
    _adjudicate(env, lease_id)
    # Leader errored, validator re-running the same work succeeds:
    # canonical handling says disagree (a new leader must be picked).
    assert (
        env.vm.run_validator(
            leader_error=gl_vm.UserError("[EXPECTED] simulated leader failure")
        )
        is False
    )
