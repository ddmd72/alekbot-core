"""
SlotExclusion — removed in Prompt Builder v4.

The slot exclusion concept (blocking a named slot from rendering) is no longer
needed. In v4, agent profiles define exactly which tokens are active. A token
that is not in the profile simply does not render.

This file is kept as a stub to avoid import errors in legacy test code during
the migration period. Delete it once all references are removed.
"""
