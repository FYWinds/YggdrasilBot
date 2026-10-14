from yggdrasil.llm.catalog import ModelCaps
from yggdrasil.llm.registry import display_name
from yggdrasil.llm.streaming import RequestStat, RunStats
from yggdrasil.runtime import ProviderInfo


def _provider(caps: dict[str, ModelCaps]) -> ProviderInfo:
    return ProviderInfo(1, "kami", "https://kami.example/v1", None, {}, True, (), caps=caps)


def test_display_name_shows_catalog_match_without_provider_or_gateway_prefix():
    caps = ModelCaps(catalog_id="anthropic/claude-sonnet-5-5", vision=True, pdf=True, tools=True, reasoning=True)
    p = _provider({"cvp/claude-sonnet-5-5": caps})
    assert display_name(p, "cvp/claude-sonnet-5-5") == "Anthropic/claude-sonnet-5-5"
    assert display_name(p, "cvp/some-unlisted-model") == "some-unlisted-model"


def test_run_speed_skips_short_and_burst_delivered_requests():
    stats = RunStats(
        [
            RequestStat("m", ttft=0.8, generating=2.0, tokens=300),
            RequestStat("m", ttft=0.5, generating=0.3, tokens=8),  # tool call: too few tokens
            RequestStat("m", ttft=2.2, generating=0.01, tokens=200),  # proxy flushed it in one burst
        ]
    )
    assert stats.ttft == 0.8
    assert stats.tokens_per_s == 150
