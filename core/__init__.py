"""OpenWrt local build assistant."""

__version__ = "5.0"

from .models import BuildSpec, PluginSpec, PrebuiltPackageSpec, ProjectSpec, ScriptSpec

__all__ = (
    "BuildSpec",
    "PluginSpec",
    "PrebuiltPackageSpec",
    "ProjectSpec",
    "ScriptSpec",
    "__version__",
)
