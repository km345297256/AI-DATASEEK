"""Real-world CSV dialect compatibility; no network or external data needed."""
import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def tabular():
    pytest.importorskip("pandas")
    path = Path(__file__).resolve().parents[2] / "tools" / "tabular" / "operations.py"
    spec = importlib.util.spec_from_file_location("tabular_dialect_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("delimiter", [",", ";", "\t", "|"])
def test_csv_preserves_columns_and_quoted_delimiters(tabular, tmp_path, delimiter):
    path = tmp_path / "international.csv"
    path.write_text(f'name{delimiter}value\n"a{delimiter}b"{delimiter}3.5\n"c{delimiter}d"{delimiter}4.0\n')
    frame = tabular.read_table(str(path), max_rows=1)
    assert frame.shape == (1, 2)
    assert list(frame.columns) == ["name", "value"]
    assert frame.iloc[0, 0] == f"a{delimiter}b"
    assert frame.iloc[0, 1] == 3.5


def test_single_column_and_explicit_tsv_remain_compatible(tabular, tmp_path):
    single = tmp_path / "single.csv"
    single.write_text("value\n1\n2\n")
    assert tabular.read_table(str(single)).shape == (2, 1)
    tab = tmp_path / "table.tsv"
    tab.write_text("name\tvalue\na;b\t1\n")
    assert tabular.read_table(str(tab)).iloc[0, 0] == "a;b"


def test_numeric_measurements_are_not_reported_as_1970_dates(tabular):
    import pandas as pd
    profile = tabular.column_profile(pd.Series([1.4, 3.6, 8.9]))
    assert 'numeric' in profile
    assert 'time_range' not in profile
    dates = tabular.column_profile(pd.Series(['2024-01-01', '2024-02-01']))
    assert dates['time_range'] == ['2024-01-01 00:00:00', '2024-02-01 00:00:00']
