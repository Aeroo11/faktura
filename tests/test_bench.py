import pytest

from faktura.bench.cord import CordParse, MenuItem, SubTotal, Total, per_key_f1, to_gt_like
from faktura.bench.donut_metric import JSONParseEvaluator, edit_distance

GT = {
    "menu": [{"nm": "ES TEH", "cnt": "2", "price": "10.000"}, {"nm": "NASI", "price": "8.000"}],
    "total": {"total_price": "18.000", "cashprice": "20.000", "changeprice": "2.000"},
}


@pytest.mark.parametrize(
    ("a", "b", "d"),
    [
        ("", "", 0),
        ("abc", "abc", 0),
        ("kitten", "sitting", 3),
        ("10.000", "10,000", 1),
        ("", "abc", 3),
    ],
)
def test_edit_distance(a, b, d):
    assert edit_distance(a, b) == d


def test_perfect_prediction_scores_one():
    ev = JSONParseEvaluator()
    assert ev.cal_acc(GT, GT) == 1.0
    assert ev.cal_f1([GT], [GT]) == 1.0


def test_empty_prediction_scores_zero():
    ev = JSONParseEvaluator()
    assert ev.cal_acc({}, GT) == 0.0
    assert ev.cal_f1([{}], [GT]) == 0.0


def test_single_dict_menu_equals_one_item_list():
    # cord-v2 stores a one-item menu as a dict; the evaluator must treat both alike.
    ev = JSONParseEvaluator()
    gt = {"menu": {"nm": "A", "price": "1"}}
    assert ev.cal_acc({"menu": [{"nm": "A", "price": "1"}]}, gt) == 1.0


def test_one_wrong_field_lowers_f1():
    ev = JSONParseEvaluator()
    pred = {**GT, "total": {**GT["total"], "changeprice": "2.500"}}
    # 8 fields (3 + 2 + 3); one wrong counts as one FP and one FN: 7 / (7 + 2/2)
    assert ev.cal_f1([pred], [GT]) == pytest.approx(7 / 8)
    assert 0.9 < ev.cal_acc(pred, GT) < 1.0


def test_to_gt_like_drops_nulls_and_empty_lists():
    nulls = dict.fromkeys(MenuItem.model_fields, None) | {"sub": []}
    p = CordParse(
        menu=[MenuItem(**(nulls | {"nm": "ES TEH", "price": "10.000"}))],
        void_menu=[],
        sub_total=SubTotal(**dict.fromkeys(SubTotal.model_fields)),
        total=Total(**(dict.fromkeys(Total.model_fields) | {"total_price": "10.000"})),
    )
    assert to_gt_like(p) == {
        "menu": [{"nm": "ES TEH", "price": "10.000"}],
        "total": {"total_price": "10.000"},
    }


def test_streamed_images_stay_aligned_with_ground_truth(tmp_path, monkeypatch):
    import json

    import pyarrow as pa
    import pyarrow.parquet as pq

    from faktura.bench import cord

    paths = []
    for part in range(2):  # two files, like a multi-file split
        rows = range(part * 5, part * 5 + 5)
        table = pa.table(
            {
                "image": [{"bytes": f"img{i}".encode(), "path": None} for i in rows],
                "ground_truth": [
                    json.dumps({"gt_parse": {"total": {"total_price": str(i)}}}) for i in rows
                ],
            }
        )
        paths.append(str(tmp_path / f"p{part}.parquet"))
        pq.write_table(table, paths[-1], row_group_size=3)
    monkeypatch.setattr(cord, "split_files", lambda split: paths)

    gts = cord.load_ground_truth("test")
    images = list(cord.iter_images("test", batch_size=2))
    assert [g["idx"] for g in gts] == [i for i, _ in images] == list(range(10))
    for g, (_, img) in zip(gts, images, strict=True):
        assert img == f"img{g['gt']['total']['total_price']}".encode()


def test_lenient_f1_ignores_only_cosmetic_differences():
    from faktura.bench.cord import lenient_f1

    gt = {"menu": [{"cnt": "2 x", "price": "8,000", "nm": "ICE TEA"}]}
    cosmetic = {"menu": [{"cnt": "2x", "price": "8.000", "nm": "Ice Tea"}]}
    wrong = {"menu": [{"cnt": "2x", "price": "9.000", "nm": "Ice Tea"}]}
    assert lenient_f1([cosmetic], [gt]) == 1.0
    assert lenient_f1([wrong], [gt]) < 1.0
    assert JSONParseEvaluator().cal_f1([cosmetic], [gt]) == 0.0  # official metric unchanged


def test_per_key_f1_matches_global_on_single_key():
    pred = {"total": {"total_price": "18.000"}}
    gt = {"total": {"total_price": "18.000"}}
    assert per_key_f1([pred], [gt])["total.total_price"]["f1"] == 1.0
