"""Export actual Textual screens for review. No agents or terminal required.

    .venv/bin/python scripts/render_tui.py --output /tmp/multiverse-previews
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
from tui_demo import DemoBackend
from multiverse.tui import MultiverseApp
from multiverse.dashboard import group_key


async def render(output):
    os.environ.pop("NO_COLOR", None)  # Preview artifacts intentionally show the color theme.
    output.mkdir(parents=True, exist_ok=True)
    backend = DemoBackend()
    app = MultiverseApp(backend.root, backend, refresh_interval=0)
    async with app.run_test(size=(140, 32)) as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.save_screenshot("dashboard.svg", path=str(output))
        review = backend.root / "subverses/pr-review"
        app.select_key(group_key(review))
        await pilot.press("right")
        await pilot.pause(0.2)
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.save_screenshot("pr-review.svg", path=str(output))
        await pilot.press("p")
        await app.workers.wait_for_complete()
        await pilot.pause()
        for width, height in ((80, 24), (60, 20)):
            await pilot.resize_terminal(width, height)
            await pilot.pause()
            app.save_screenshot(f"compact-{width}.svg", path=str(output))
            await pilot.press("p")
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.save_screenshot(f"inspector-{width}.svg", path=str(output))
            await pilot.press("p")
        await pilot.resize_terminal(110, 28)
        await pilot.press("n", *"benchmark", "tab", "tab", *"Verify fused attention benchmarks")
        await pilot.pause()
        app.save_screenshot("spawn.svg", path=str(output))
        await pilot.press("shift+tab")
        await pilot.pause()
        app.save_screenshot("harness-selector.svg", path=str(output))
    print(f"Saved hierarchy, inspector, compact and inline creation previews to {output}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("/tmp/multiverse-previews"))
    asyncio.run(render(parser.parse_args().output))
