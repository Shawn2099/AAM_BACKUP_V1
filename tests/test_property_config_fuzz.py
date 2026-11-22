"""Config loader fuzz (Hypothesis): a hostile or typo'd config.yaml must be refused cleanly (pydantic
ValidationError / ValueError) - never an unrelated crash, never a hang - and an accepted config must
round-trip. A config that stops the services at boot should say why."""
import copy
import tempfile
from pathlib import Path

import yaml
from hypothesis import HealthCheck, given, settings, strategies as st

from test_support.sandbox_config import _config_dict
from models.config import AppConfig, load_config

_ROOT = Path(tempfile.mkdtemp(prefix="aam_cfgfuzz_"))
_BASE = _config_dict(_ROOT)

BAD = st.one_of(
    st.none(), st.booleans(), st.integers(-2**40, 2**40), st.floats(allow_nan=True, allow_infinity=True),
    st.text(max_size=40), st.just(""), st.just("\x00"), st.just("FY26-26"), st.just("\\\\host\\share\\FY99-99"),
    st.lists(st.integers(), max_size=3), st.dictionaries(st.text(max_size=4), st.integers(), max_size=2),
    st.just("C:\\\\"), st.just("../../etc"), st.just(" "), st.just("a" * 5000), st.just(-1), st.just(0),
)


def _leaf_paths(d, prefix=()):
    for k, v in d.items():
        if isinstance(v, dict):
            yield from _leaf_paths(v, prefix + (k,))
        else:
            yield prefix + (k,)


_PATHS = sorted(_leaf_paths(_BASE))


def test_base_config_is_valid():
    p = _ROOT / "base.yaml"
    p.write_text(yaml.safe_dump(_BASE), encoding="utf-8")
    assert load_config(str(p))


@settings(max_examples=400, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(path=st.sampled_from(_PATHS), value=BAD, extra=st.booleans())
def test_mutated_config_is_refused_cleanly_or_accepted(path, value, extra):
    data = copy.deepcopy(_BASE)
    node = data
    for k in path[:-1]:
        node = node[k]
    node[path[-1]] = value
    if extra:
        data["unknown_section_xyz"] = {"a": 1}
    p = _ROOT / "mut.yaml"
    try:
        p.write_text(yaml.safe_dump(data, allow_unicode=False), encoding="utf-8")
    except Exception:
        return  # not representable in YAML at all
    try:
        cfg = load_config(str(p))
    except ValueError:           # pydantic.ValidationError is a ValueError
        assert True
        return
    assert not extra, "unknown top-level section must be refused (StrictModel)"
    assert isinstance(cfg, AppConfig)
    # accepted -> must survive a dump/load round trip unchanged
    again = AppConfig.model_validate(cfg.model_dump(mode="json"))
    assert again.model_dump(mode="json") == cfg.model_dump(mode="json")
