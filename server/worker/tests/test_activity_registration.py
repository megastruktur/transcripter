"""Registration guard: every @activity.defn must be in main.ACTIVITIES.

An activity that exists but is not in the Worker(activities=[...]) list
fails workflows at runtime with NotFoundError while the stage row sits
'pending' forever — observed live 2026-08-27 when the enrich stage was
added to the workflow but not to the registration list.
"""

import worker.activities as activities_mod
import worker.main as main_mod


def test_all_activities_registered() -> None:
    defined = {
        name
        for name, fn in vars(activities_mod).items()
        if callable(fn) and hasattr(fn, "__temporal_activity_definition")
    }
    registered = {fn.__name__ for fn in main_mod.ACTIVITIES}
    assert defined == registered, (
        f"drift: defined-not-registered={sorted(defined - registered)}, "
        f"registered-not-defined={sorted(registered - defined)}"
    )
