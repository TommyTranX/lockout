"""Lockout command line."""

from __future__ import annotations

import json
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from lockout import config, graph, urns

app = typer.Typer(
    add_completion=False,
    help="Lockout/tagout for data pipelines — block ML training on bad upstream data.",
)
console = Console()


@app.command()
def seed() -> None:
    """Seed the ML subgraph, lineage, and Lockout's structured properties."""
    from lockout.seed import ml_subgraph

    counts = ml_subgraph.seed()
    console.print("[bold green]seeded[/]")
    for key, value in counts.items():
        console.print(f"  {key:<24} {value}")
    console.print(f"\n  model: {urns.model()}")


@app.command()
def arm() -> None:
    """Evaluate every rule and write the results into DataHub as assertions."""
    from lockout.policy import rules
    from lockout.writeback import assertions

    results = rules.evaluate_all()
    armed = assertions.arm(results)

    table = Table(title="assertions armed", show_lines=False)
    for column in ("rule", "target", "observed", "threshold", "result"):
        table.add_column(column)
    for a in armed:
        table.add_row(
            a["rule"],
            f"{a['table']}.{a['column']}",
            str(a["observed"]),
            str(a["threshold"]),
            "[green]PASS[/]" if a["passed"] else "[red]FAIL[/]",
        )
    console.print(table)
    failed = [a for a in armed if not a["passed"]]
    console.print(
        f"\n{len(armed)} armed, [red]{len(failed)} failing[/]"
        if failed
        else f"\n{len(armed)} armed, all passing"
    )


@app.command()
def permit(
    model: str = typer.Option(None, help="Model URN. Defaults to the demo model."),
    json_out: bool = typer.Option(False, "--json", help="Emit the permit as JSON."),
) -> None:
    """Ask Lockout whether a training run may start."""
    from lockout.policy import decision

    result = decision.request_permit(model)
    if json_out:
        console.print_json(json.dumps(result.to_dict()))
    else:
        console.print(result.render())
    raise typer.Exit(0 if result.granted else 1)


@app.command()
def train(
    no_lockout: bool = typer.Option(
        False, "--no-lockout", help="Force the run through even if the permit is denied."
    ),
    out: str = typer.Option(None, help="Directory to write measured artifacts to."),
    commit: bool = typer.Option(True, help="Write the decision back into DataHub."),
) -> None:
    """Run training — after asking for a permit."""
    from lockout.policy import decision
    from lockout.training import job
    from lockout.writeback import state

    run_id = f"run-{int(time.time())}"
    result = decision.request_permit()
    console.print(result.render())
    console.print()

    if not result.granted and not no_lockout:
        if commit:
            written = state.commit_decision(result, run_id)
            console.print("[bold]written back to DataHub:[/]")
            for key, value in written.items():
                if value:
                    console.print(f"  {key:<10} {value}")
        outcome = job.TrainingResult(
            run_id=run_id, trained=False, reason="blocked by Lockout"
        )
        if out:
            console.print(f"\nartifacts: {job.write_artifacts(outcome, out)}")
        console.print("\n[red]training did not start[/]")
        raise typer.Exit(1)

    if not result.granted:
        console.print("[yellow]--no-lockout: overriding a DENIED permit[/]\n")

    outcome = job.train(run_id)
    console.print(f"[bold]training {'completed' if outcome.trained else 'failed'}[/]")
    console.print(f"  rows        {outcome.rows_used} ({outcome.train_rows} train / {outcome.test_rows} test)")
    if outcome.mae is not None:
        console.print(f"  MAE         {outcome.mae:,.2f}")
        console.print(f"  RMSE        {outcome.rmse:,.2f}")
        console.print(f"  wall clock  {outcome.wall_clock_s}s")

    if commit:
        metrics = {"mae": outcome.mae, "rmse": outcome.rmse} if outcome.mae else None
        state.record_run(result, run_id, metrics)
    if out:
        console.print(f"\nartifacts: {job.write_artifacts(outcome, out)}")


@app.command()
def watch(
    timeout: float = typer.Option(120.0, help="Seconds to watch before exiting."),
    from_beginning: bool = typer.Option(False, help="Replay the topic from the start."),
) -> None:
    """Subscribe to DataHub's MetadataChangeLog and re-check on every relevant change."""
    from lockout.events import consumer
    from lockout.policy import rules
    from lockout.writeback import assertions

    armed_urns = {
        urns.dataset(t)
        for t in (config.RAW_TABLE, config.STAGING_TABLE, config.MART_TABLE)
    }
    console.print(f"[bold]watching[/] {consumer.TOPIC} for {len(armed_urns)} armed datasets")
    console.print("  (no polling loop — this is DataHub's own change stream)\n")

    seen = 0
    for change in consumer.watch(
        interesting=consumer.watches_datasets(armed_urns),
        from_beginning=from_beginning,
        timeout_s=timeout,
    ):
        seen += 1
        console.print(
            f"[cyan]MCL[/] {change.change_type} {change.aspect_name} "
            f"on {urns.table_of(change.entity_urn)} "
            f"({'update' if change.is_update else 'create'})"
        )
        results = rules.evaluate_all()
        armed = assertions.arm(results)
        failing = [a for a in armed if not a["passed"]]
        console.print(
            f"    re-evaluated {len(armed)} assertions — "
            + (f"[red]{len(failing)} failing[/]" if failing else "[green]all passing[/]")
        )
    console.print(f"\nsaw {seen} relevant change(s)")


@app.command()
def status() -> None:
    """Show what Lockout currently knows."""
    from lockout.policy import decision

    result = decision.request_permit()
    table = Table(title="lockout status")
    table.add_column("field")
    table.add_column("value")
    table.add_row("model", urns.model())
    table.add_row("verdict", "[green]GRANTED[/]" if result.granted else "[red]DENIED[/]")
    table.add_row("features checked", str(len(result.features_checked)))
    table.add_row("upstream datasets", str(len(result.upstream_datasets)))
    table.add_row("failing assertions", str(len(result.evidence)))
    table.add_row("decision time", f"{result.elapsed_ms} ms")
    console.print(table)
    for d in result.upstream_datasets:
        console.print(f"  upstream: {d}")


@app.command()
def doctor() -> None:
    """Check the environment before a demo.

    Exists because DataHub can enter a state where writes succeed but the search and
    graph indexes silently stop updating — the MAE consumer drops out of its Kafka
    group and nothing surfaces the failure. Everything downstream then looks empty for
    no visible reason, so this probes the indexing path explicitly.
    """
    ok = True

    try:
        version = graph.client().server_config.raw_config["versions"]["acryldata/datahub"]["version"]
        console.print(f"[green]✓[/] GMS reachable at {config.GMS_URL} ({version})")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]✗[/] GMS unreachable: {exc}")
        raise typer.Exit(1)

    model_urn = urns.model()
    upstream = graph.upstream_datasets(model_urn)
    if upstream:
        console.print(f"[green]✓[/] graph index live — model resolves {len(upstream)} upstream dataset(s)")
        for hit in upstream:
            console.print(f"      degree {hit['degree']}  {hit['urn']}")
    else:
        ok = False
        console.print(
            "[red]✗[/] model has no upstream lineage.\n"
            "      Either `lockout seed` has not run, or the graph index is stale.\n"
            "      If seeding ran, the MAE consumer has likely stalled — the reliable\n"
            "      recovery is `datahub docker nuke && datahub docker quickstart`."
        )

    db = Path(config.DB_PATH)
    if db.exists():
        console.print(f"[green]✓[/] sample database present ({db.stat().st_size // 1_000_000} MB)")
    else:
        ok = False
        console.print(f"[red]✗[/] missing {db} — run `make data`")

    raise typer.Exit(0 if ok else 1)


if __name__ == "__main__":
    app()
