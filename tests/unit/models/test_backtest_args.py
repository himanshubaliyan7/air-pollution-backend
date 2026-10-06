import pandas as pd
import pytest

from scripts.daily_verdict_backtest import FOLDS, parse_folds


def test_no_fold_arguments_keep_the_delhi_folds():
    assert parse_folds([]) is FOLDS and list(FOLDS) == ["onset", "winter"]


def test_folds_are_start_end_pairs_with_an_optional_name():
    folds = parse_folds(["monsoon=2026-06-15:2026-09-15", "2026-01-01:2026-02-01"])
    assert folds["monsoon"] == (pd.Timestamp("2026-06-15"), pd.Timestamp("2026-09-15"))
    assert folds["2026-01-01..2026-02-01"] == (pd.Timestamp("2026-01-01"), pd.Timestamp("2026-02-01"))


@pytest.mark.parametrize("spec", ["2026-01-01", "2026-02-01:2026-01-01", "x=:"])
def test_bad_folds_are_refused(spec):
    with pytest.raises(ValueError):
        parse_folds([spec])
