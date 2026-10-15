"""Rewrite uses of `temporalio.workflow.X` and `temporalio.activity.X` that are not decorators or calls.

Three outcomes for `workflow.X`:

- Mistral has the same function: `workflow.sleep` stays `workflow.sleep` (now Mistral's `workflow`).
- Mistral has no wrapper for it: it is kept through Temporal itself, as
  `temporal_workflow.X`. This works because Mistral workers run on Temporal.
- It is a decorator or a call rewritten by workflows.py or activities.py: it is
  turned into `temporal_workflow.X` here, then replaced by those modules.
"""

from __future__ import annotations

import libcst as cst

from .context import TEMPORAL_ACTIVITY_ALIAS, TEMPORAL_WORKFLOW_ALIAS, FileContext

WORKFLOW_MODULE = "temporalio.workflow"
ACTIVITY_MODULE = "temporalio.activity"

SAME_NAME_ON_MISTRAL = {"wait_condition", "sleep", "now", "uuid4", "random", "unsafe"}

REWRITTEN_ELSEWHERE = {
    "defn",
    "run",
    "signal",
    "query",
    "update",
    "init",
    "continue_as_new",
    "execute_child_workflow",
    "start_child_workflow",
    "execute_activity",
    "start_activity",
    "execute_local_activity",
    "start_local_activity",
    "execute_activity_method",
    "start_activity_method",
    "execute_local_activity_method",
    "start_local_activity_method",
    "execute_activity_class",
    "start_activity_class",
}

MISTRAL_EQUIVALENTS = {
    "upsert_search_attributes": "workflow.add_search_keys(...) (Mistral search keys)",
    "upsert_typed_search_attributes": "workflow.add_search_keys(...) (Mistral search keys)",
    "set_signal_handler": "a method decorated with @workflow.signal(), so Mistral lists it in the workflow schema",
    "set_query_handler": "a method decorated with @workflow.query(), so Mistral lists it in the workflow schema",
    "set_update_handler": "a method decorated with @workflow.update(), so Mistral lists it in the workflow schema",
    "set_dynamic_signal_handler": "one @workflow.signal() method per signal name",
    "set_dynamic_query_handler": "one @workflow.query() method per query name",
    "set_dynamic_update_handler": "one @workflow.update() method per update name",
}

NEXUS_APIS = {"create_nexus_client", "NexusClient", "NexusOperationHandle"}

# These need a running workflow: fine under a Mistral worker, not in the SDK's local mode.
NEEDS_A_WORKER = {"logger", "info", "memo", "all_handlers_finished", "patched", "deprecate_patch"}


def rewrite_temporal_attribute(ctx: FileContext, original: cst.Attribute, updated: cst.Attribute) -> cst.BaseExpression:
    base = ctx.full_name(original.value)
    member = original.attr.value
    if base == WORKFLOW_MODULE:
        ctx.changed = True
        return _workflow_member(ctx, original, member)
    if base == ACTIVITY_MODULE:
        ctx.changed = True
        return _activity_member(ctx, original, member)
    if base == WORKFLOW_MODULE + ".unsafe":
        return _unsafe_member(ctx, original, member)
    return updated


def _workflow_member(ctx: FileContext, node: cst.Attribute, member: str) -> cst.BaseExpression:
    if member in SAME_NAME_ON_MISTRAL:
        ctx.need("mistralai.workflows", "workflow")
        return cst.parse_expression(f"workflow.{member}")

    ctx.need("temporalio", "workflow", TEMPORAL_WORKFLOW_ALIAS)
    kept = cst.parse_expression(f"{TEMPORAL_WORKFLOW_ALIAS}.{member}")
    if member in REWRITTEN_ELSEWHERE:
        return kept

    if member in NEXUS_APIS:
        ctx.report(
            "T2M062", node, f"`workflow.{member}` (Temporal Nexus) has no Mistral equivalent", once_per_file=True
        )
    elif member in MISTRAL_EQUIVALENTS:
        ctx.report(
            "T2M061",
            node,
            f"`workflow.{member}` kept from Temporal",
            f"Mistral equivalent: {MISTRAL_EQUIVALENTS[member]}",
            once_per_file=True,
        )
    else:
        hint = None
        if member in NEEDS_A_WORKER:
            hint = "Works under a Mistral worker; raises in the SDK's local mode (execute_workflow outside a worker)."
        ctx.report(
            "T2M060",
            node,
            f"`workflow.{member}` is used through `{TEMPORAL_WORKFLOW_ALIAS}` (Mistral workflows run on Temporal)",
            hint,
            once_per_file=True,
        )
    return kept


def _activity_member(ctx: FileContext, node: cst.Attribute, member: str) -> cst.BaseExpression:
    if member == "heartbeat":
        ctx.need("mistralai.workflows", "activity_heartbeat")
        return cst.Name("activity_heartbeat")

    ctx.need("temporalio", "activity", TEMPORAL_ACTIVITY_ALIAS)
    kept = cst.parse_expression(f"{TEMPORAL_ACTIVITY_ALIAS}.{member}")
    if member == "defn":
        return kept  # rewritten by activities.py
    if member == "raise_complete_async":
        ctx.report(
            "T2M062",
            node,
            "`activity.raise_complete_async` (asynchronous activity completion) is not supported by Mistral",
            "Turn the activity into a workflow waiting for a signal or update instead.",
            once_per_file=True,
        )
    else:
        ctx.report(
            "T2M060",
            node,
            f"`activity.{member}` is used through `{TEMPORAL_ACTIVITY_ALIAS}` (Mistral activities run on Temporal)",
            once_per_file=True,
        )
    return kept


def _unsafe_member(ctx: FileContext, node: cst.Attribute, member: str) -> cst.BaseExpression:
    if member == "imports_passed_through":
        return cst.parse_expression("workflow.unsafe.imports_passed_through")
    if member == "sandbox_unrestricted":
        return cst.parse_expression("workflow.unsafe.skip_determinism_enforcement")
    ctx.need("temporalio", "workflow", TEMPORAL_WORKFLOW_ALIAS)
    ctx.report("T2M060", node, f"`workflow.unsafe.{member}` kept from Temporal", once_per_file=True)
    return cst.parse_expression(f"{TEMPORAL_WORKFLOW_ALIAS}.unsafe.{member}")
