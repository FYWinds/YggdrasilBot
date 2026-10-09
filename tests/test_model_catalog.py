from yggdrasil.llm.catalog import Catalog

_TEXT = {"modalities": {"input": ["text"]}, "tool_call": True, "limit": {"context": 8000}}
_SEEING = {"modalities": {"input": ["text", "image", "pdf"]}, "tool_call": True, "limit": {"context": 1_000_000}}

CATALOG = Catalog(
    {
        # Aggregator listed first with wrong data: the vendor's own entry must still win.
        "openrouter": {"models": {"claude-x": _TEXT, "anthropic/claude-x": _TEXT}},
        "anthropic": {"models": {"claude-x": _SEEING}},
        "deepseek": {"models": {"deepseek-chat": _TEXT}},
    }
)


def test_gateway_prefixes_are_stripped_and_vendor_entry_preferred() -> None:
    caps = CATALOG.lookup("cvp/claude-x")
    assert caps is not None
    assert (caps.catalog_id, caps.vision, caps.pdf, caps.context) == ("anthropic/claude-x", True, True, 1_000_000)


def test_exact_provider_model_key_wins_over_bare_match() -> None:
    caps = CATALOG.lookup("openrouter/claude-x")
    assert caps is not None
    assert caps.catalog_id == "openrouter/claude-x"
    assert caps.vision is False


def test_alias_overrides_and_unmatched_ids_are_left_out() -> None:
    out = CATALOG.resolve(["my-model", "cvp/claude-x", "unknown"], {"my-model": "deepseek/deepseek-chat"})
    assert set(out) == {"my-model", "cvp/claude-x"}
    assert out["my-model"]["catalog_id"] == "deepseek/deepseek-chat"
    assert out["my-model"]["vision"] is False


def test_alias_that_does_not_resolve_does_not_fall_back_to_the_id() -> None:
    assert CATALOG.resolve(["cvp/claude-x"], {"cvp/claude-x": "nope/missing"}) == {}
