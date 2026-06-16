"""openfde.plugins_runtime — built-in plugin RUNTIMES (Plugin Registry v1-H).

Each module here is a lightweight runtime that a built-in/suggested plugin spec points at via its
``runtime`` descriptor. The activation API (``openfde.plugins.load_plugin_runtime`` /
``runtime_for_capability``) imports these LAZILY — only when a repo-matching plugin's capability is
actually requested — so listing ``/api/plugins`` never loads them. Runtimes DELEGATE to the canonical
core implementation; no analysis logic is duplicated here.
"""
