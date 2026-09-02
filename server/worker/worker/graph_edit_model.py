"""GraphEdit catalog rows (graph edit-store, Phase A).

Shared by the API (validation, audit reads) and the worker (edit
activities). The table is created by the API's ``create_all`` (the
worker never creates schema — same contract as ``stages``), so BOTH
sides must declare the model. The canonical definition lives here as a
plain column-spec helper; each side binds it to its own ``Base``
(separate venvs, no cross-package imports).
"""

from __future__ import annotations

import enum


class EditStatus(enum.Enum):
    applied = "applied"
    orphaned = "orphaned"
    retired = "retired"


class EditTarget(enum.Enum):
    event = "event"
    entity = "entity"
    relation = "relation"


class EditOp(enum.Enum):
    update = "update"
    delete = "delete"
    create = "create"
    merge = "merge"
