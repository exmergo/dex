"""The spellings a model-name list accepts (#1243).

A scope argument is a list of model names, and the list has more than one
spelling: separate arguments, comma-separated, or one quoted argument with the
names separated by spaces. All three name the same models.
"""

from __future__ import annotations

from exmergo_dex_core.model_scope import model_names


def test_separate_arguments_name_every_model():
    assert model_names(["a", "b"]) == ["a", "b"]


def test_a_comma_separated_list_names_every_model():
    assert model_names(["a,b"]) == ["a", "b"]
    assert model_names(["a, b"]) == ["a", "b"]


def test_a_quoted_list_names_every_model():
    """`dex maintain verify "a b"` is two models, not one named "a b"."""
    assert model_names(["a b"]) == ["a", "b"]


def test_a_mixed_list_names_every_model():
    assert model_names(["a b", "c"]) == ["a", "b", "c"]
    assert model_names(["a,b", "c d"]) == ["a", "b", "c", "d"]


def test_the_case_of_a_name_is_kept():
    """The caller folds the case, the way it matches a model name."""
    assert model_names(["Dim_Customers"]) == ["Dim_Customers"]


def test_a_list_that_names_nothing_is_empty():
    assert model_names([]) == []
    assert model_names(None) == []
    assert model_names([""]) == []
    assert model_names([" , "]) == []


def test_the_order_the_names_were_given_is_kept():
    assert model_names(["c", "a b"]) == ["c", "a", "b"]
