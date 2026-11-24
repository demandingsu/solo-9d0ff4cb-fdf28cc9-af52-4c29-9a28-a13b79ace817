"""台账校验与核对包汇总测试。"""
import pytest

from app import ledger


def parse(text_in, text_tr, text_de):
    return ledger.parse_batch(
        text_in.encode("utf-8"), text_tr.encode("utf-8"), text_de.encode("utf-8")
    )


def test_parse_rejects_non_integer_crates():
    with pytest.raises(ledger.BatchRejected) as exc:
        parse("仓次,果园,箱数\nS1,果园A,10.5\n", "起仓次,讫仓次,箱数\n", "仓次,类型,去向,箱数\n")
    assert any("整箱" in p for p in exc.value.problems)


def test_parse_rejects_bad_header():
    with pytest.raises(ledger.BatchRejected) as exc:
        parse("仓次,果园,数量\nS1,果园A,10\n", "起仓次,讫仓次,箱数\n", "仓次,类型,去向,箱数\n")
    assert any("表头" in p for p in exc.value.problems)


def test_parse_rejects_bad_kind_and_self_transfer():
    with pytest.raises(ledger.BatchRejected) as exc:
        parse(
            "仓次,果园,箱数\nS1,果园A,10\n",
            "起仓次,讫仓次,箱数\nS1,S1,5\n",
            "仓次,类型,去向,箱数\nS1,退货,市场甲,10\n",
        )
    assert any("起讫仓次相同" in p for p in exc.value.problems)
    assert any("类型只能是" in p for p in exc.value.problems)


def test_balance_discrepancy_names_session():
    batch = parse(
        "仓次,果园,箱数\nS1,果园A,50\n",
        "起仓次,讫仓次,箱数\nS1,S2,20\n",
        "仓次,类型,去向,箱数\nS1,交货,市场甲,60\nS2,交货,市场乙,25\n",
    )
    with pytest.raises(ledger.BatchRejected) as exc:
        ledger.validate_batch(batch, set(), set())
    assert any("S1" in p and "差额" in p and "30" in p for p in exc.value.problems)
    assert any("S2" in p and "差额" in p and "5" in p for p in exc.value.problems)


def test_mixed_orchard_session_rejected():
    batch = parse(
        "仓次,果园,箱数\nS1,果园A,10\nS1,果园B,10\n",
        "起仓次,讫仓次,箱数\n",
        "仓次,类型,去向,箱数\nS1,交货,市场甲,20\n",
    )
    with pytest.raises(ledger.BatchRejected) as exc:
        ledger.validate_batch(batch, set(), set())
    assert any("多个果园" in p for p in exc.value.problems)


def test_transfer_across_orchards_rejected():
    batch = parse(
        "仓次,果园,箱数\nS1,果园A,10\nS2,果园B,10\n",
        "起仓次,讫仓次,箱数\nS1,S2,5\n",
        "仓次,类型,去向,箱数\nS1,交货,市场甲,5\nS2,交货,市场乙,15\n",
    )
    with pytest.raises(ledger.BatchRejected) as exc:
        ledger.validate_batch(batch, set(), set())
    assert any("不同果园" in p for p in exc.value.problems)


def test_too_many_destinations_rejected():
    deliveries = "仓次,类型,去向,箱数\n" + "".join(
        f"S1,交货,去向{i},10\n" for i in range(1, 6)
    )
    batch = parse("仓次,果园,箱数\nS1,果园A,50\n", "起仓次,讫仓次,箱数\n", deliveries)
    with pytest.raises(ledger.BatchRejected) as exc:
        ledger.validate_batch(batch, set(), set())
    assert any("最多 4 个去向" in p for p in exc.value.problems)


def test_destination_cap_counts_existing_ones():
    batch = parse(
        "仓次,果园,箱数\nS1,果园A,10\n",
        "起仓次,讫仓次,箱数\n",
        "仓次,类型,去向,箱数\nS1,交货,新去向,10\n",
    )
    with pytest.raises(ledger.BatchRejected) as exc:
        ledger.validate_batch(batch, set(), {"去向1", "去向2", "去向3", "去向4"})
    assert any("最多 4 个去向" in p for p in exc.value.problems)


def test_reused_session_rejected():
    batch = parse(
        "仓次,果园,箱数\nS1,果园A,10\n",
        "起仓次,讫仓次,箱数\n",
        "仓次,类型,去向,箱数\nS1,交货,市场甲,10\n",
    )
    with pytest.raises(ledger.BatchRejected) as exc:
        ledger.validate_batch(batch, {"S1"}, set())
    assert any("已入账" in p for p in exc.value.problems)


def test_transfer_chain_attributes_orchard():
    """S1(果园A) -> S2 -> S3，S3 的交货应归属果园A。"""
    batch = parse(
        "仓次,果园,箱数\nS1,果园A,10\n",
        "起仓次,讫仓次,箱数\nS1,S2,10\nS2,S3,10\n",
        "仓次,类型,去向,箱数\nS3,交货,市场甲,10\n",
    )
    mapping = ledger.validate_batch(batch, set(), set())
    assert mapping["S3"] == "果园A"
    assert mapping["S2"] == "果园A"
