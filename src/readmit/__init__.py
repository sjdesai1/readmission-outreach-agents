"""readmit: synthetic readmission risk model + multi-agent patient outreach."""

import contextlib
import importlib
import warnings

# authlib (pulled in by fastmcp) forces its deprecation warnings to "always" show when it is imported,
# which overrides any filter set earlier. Load it first, then add our filter on top so ours wins.
with contextlib.suppress(ImportError):
    importlib.import_module("authlib.deprecate")
warnings.filterwarnings("ignore", message="authlib.jose module is deprecated")
