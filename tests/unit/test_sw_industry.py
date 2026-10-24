import pytest

from services.sw_industry import load_sw_membership, sw_catalog


class FakeCollection:
    def __init__(self, docs):
        self.docs = list(docs)

    @staticmethod
    def _matches(doc, query):
        for key, expected in query.items():
            if key == "$or":
                if not any(FakeCollection._matches(doc, sub) for sub in expected):
                    return False
                continue
            actual = doc.get(key)
            if isinstance(expected, dict):
                if "$in" in expected and actual not in expected["$in"]:
                    return False
                if "$lte" in expected:
                    if actual is None or str(actual) > str(expected["$lte"]):
                        return False
                if "$gte" in expected:
                    if actual is None or str(actual) < str(expected["$gte"]):
                        return False
            elif actual != expected:
                return False
        return True

    def find(self, query=None, projection=None):
        rows = [doc for doc in self.docs if self._matches(doc, query or {})]
        if projection:
            keep = [key for key, value in projection.items() if value == 1]
            rows = [{key: doc.get(key) for key in keep if key in doc} for doc in rows]
        return rows


def _member(con_code, l1_code, l1_name, l2_code, l2_name, **extra):
    doc = {
        "con_code": con_code,
        "src": "SW2021",
        "is_new": "Y",
        "l1_code": l1_code,
        "l1_name": l1_name,
        "l2_code": l2_code,
        "l2_name": l2_name,
    }
    doc.update(extra)
    return doc


@pytest.mark.unit
def test_load_sw_membership_current_snapshot_maps_l1_l2():
    collections = {
        "sw_index_member": FakeCollection(
            [
                _member(
                    "000001.SZ",
                    "801780.SI",
                    "银行",
                    "801781.SI",
                    "股份制银行Ⅱ",
                    in_date="20000629",
                ),
                _member(
                    "600519.SH",
                    "801120.SI",
                    "食品饮料",
                    "801121.SI",
                    "白酒Ⅱ",
                    in_date="20010827",
                ),
            ]
        )
    }

    mapping = load_sw_membership(collections, ["000001.SZ", "600519.SH"])

    assert mapping["000001.SZ"] == {
        "l1_code": "801780.SI",
        "l1_name": "银行",
        "l2_code": "801781.SI",
        "l2_name": "股份制银行Ⅱ",
    }
    assert mapping["600519.SH"]["l1_name"] == "食品饮料"


@pytest.mark.unit
def test_load_sw_membership_pit_uses_score_date_stint():
    """A stock that moved industries must resolve to the stint alive on as_of."""
    collections = {
        "sw_index_member": FakeCollection(
            [
                _member(
                    "000001.SZ",
                    "801010.SI",
                    "农林牧渔",
                    "801011.SI",
                    "种植业",
                    in_date="20000629",
                    out_date="20211231",
                    is_new="N",
                ),
                _member(
                    "000001.SZ",
                    "801780.SI",
                    "银行",
                    "801781.SI",
                    "股份制银行Ⅱ",
                    in_date="20220101",
                ),
            ]
        )
    }

    old = load_sw_membership(collections, ["000001.SZ"], as_of="20210601")
    new = load_sw_membership(collections, ["000001.SZ"], as_of="20230101")

    assert old["000001.SZ"]["l1_name"] == "农林牧渔"
    assert new["000001.SZ"]["l1_name"] == "银行"


@pytest.mark.unit
def test_load_sw_membership_out_date_is_inclusive():
    collections = {
        "sw_index_member": FakeCollection(
            [
                _member(
                    "000001.SZ",
                    "801010.SI",
                    "农林牧渔",
                    "801011.SI",
                    "种植业",
                    in_date="20000629",
                    out_date="20211231",
                    is_new="N",
                )
            ]
        )
    }

    assert load_sw_membership(
        collections, ["000001.SZ"], as_of="20211231"
    )["000001.SZ"]["l1_name"] == "农林牧渔"
    assert load_sw_membership(collections, ["000001.SZ"], as_of="20220101") == {}


@pytest.mark.unit
def test_load_sw_membership_accepts_bare_symbols_and_missing_collection():
    collections = {
        "sw_index_member": FakeCollection(
            [
                _member(
                    "000001.SZ",
                    "801780.SI",
                    "银行",
                    "801781.SI",
                    "股份制银行Ⅱ",
                    in_date="20000629",
                )
            ]
        )
    }

    assert load_sw_membership(collections, ["000001"])["000001"]["l1_name"] == "银行"
    assert load_sw_membership({}, ["000001"]) == {}
    assert load_sw_membership(None, ["000001"]) == {}


@pytest.mark.unit
def test_sw_catalog_sorted_with_parent_fields():
    classify = FakeCollection(
        [
            {
                "src": "SW2021",
                "level": "L1",
                "index_code": "801780.SI",
                "industry_code": "480000",
                "industry_name": "银行",
                "parent_code": "0",
            },
            {
                "src": "SW2021",
                "level": "L1",
                "index_code": "801010.SI",
                "industry_code": "110000",
                "industry_name": "农林牧渔",
                "parent_code": "0",
            },
            {
                "src": "SW2021",
                "level": "L2",
                "index_code": "801781.SI",
                "industry_code": "480100",
                "industry_name": "股份制银行Ⅱ",
                "parent_code": "480000",
            },
        ]
    )
    collections = {"sw_index_classify": classify}

    l1 = sw_catalog(collections, "l1")
    l2 = sw_catalog(collections, "l2")

    assert [row["code"] for row in l1] == ["801010.SI", "801780.SI"]
    assert l1[0]["industry_code"] == "110000"
    assert l2 == [
        {
            "code": "801781.SI",
            "name": "股份制银行Ⅱ",
            "industry_code": "480100",
            "parent_code": "480000",
            "level": "L2",
        }
    ]
    assert sw_catalog(collections, "bogus") == []
