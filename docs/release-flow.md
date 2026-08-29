# Release flow

How a change gets from a branch to a published release, and the four rules that
exist because breaking them cost real rework.

Every rule below is written the same way: what to do, then what went wrong when
it was not done. None of them are hypothetical.

## The flow

1. **Branch** off `main`. Never commit to `main` directly.
2. **Build it**, tests first. `pytest` with no path argument (see *Development*
   in the README).
3. **Open a PR.** Wait for both workflows — *Tests* (ruff + pytest) and
   *Validate* (hassfest + HACS).
4. **Pick the version.** Re-read the remote first — see rule 1.
5. **Tag a release candidate** and validate it on hardware — see rule 3.
6. **Merge**, then tag the final version and publish the release.
7. **Answer the issue** the work came from, if there is one — see rule 2.

## Rule 1 — re-read the remote immediately before tagging or merging

`git fetch --prune && gh release list` and `gh pr view <n> --json mergeable`.
Not at the start of the session: immediately before the operation.

> On 2026-08-28 a branch was numbered 0.4.9 against a `main` that had reached
> 0.5.0 in the meantime, and then folded into 0.5.0 on the strength of a check
> made eight minutes earlier — 0.5.0 was tagged and released in that gap. The
> result was a `v0.5.0-rc1` that carried *more* than the released `v0.5.0` and
> therefore sorted *below* it under semver: a pre-release HACS would offer to
> nobody. Deleting it and renumbering cost eleven edits, nine of them prose
> ("gated on the company identifier until …") rather than version strings.

This repository can have more than one agent session working in it. Assume the
remote moved.

## Rule 2 — no `Closes #N` for an issue awaiting someone else's confirmation

Put the reference in the body as plain text (`See #7`) and close the issue by
hand once the reporter has confirmed.

> The squash-merge body for #15 opened with the closing keyword and that issue's
> number. GitHub acted on it and closed the reporter's issue minutes after a
> comment on that same issue had said it would stay open until they confirmed the
> fix on *their* lamp. The timeline contradicted the comment, and reopening added
> a third notification.
>
> It then happened a second time, in the commit that first wrote this rule down:
> the PR body quoted the offending phrase inside backticks to explain it. **A
> commit message is plain text — backticks protect nothing**, and the parser
> closed the issue again. So do not reproduce a live keyword-and-number pair
> anywhere in a commit or PR body, not even as an example. Name the keyword and
> the number separately, as this paragraph does.

A fix verified on the maintainer's hardware is not verified on the reporter's.
Their lamp is the one the bug was reported against.

## Rule 3 — hardware-validate before tagging the final version

Publish a `vX.Y.Z-rcN` **pre-release**, install it through HACS (*Redownload* →
*Show beta versions*), and run the checks the release notes list. Tag the final
version only after they pass.

Every release since v0.1.1 has been validated on a Häfele Connect Mesh lamp
through an ESPHome Bluetooth proxy. Keep the rc published afterwards: it is the
record of what was validated.

Write the checks into the rc's release notes, and write them so a failure is
*visible*. The check worth the most is always the one whose failure would be
silent — for the per-lamp CTL option that was "untick it, restart Home
Assistant, confirm it is still unticked", because a re-running seed would have
re-ticked the box with nothing in any log to say so.

## Rule 4 — identify entities by the integration, not by name

When driving a live Home Assistant to validate, list the integration's own
entities. Do not match on a friendly name.

> A search for "connect" matched `light.connectivity_kit_ledbox` — an **Overkiz**
> lamp — and a `light.turn_on` against it returned a 500 that was briefly read as
> a fault in this integration. The mesh lamp was `light.television`
> ("Boîtier Mesh"). The traceback named `components/overkiz/light.py`, which is
> what settled it.

Read the traceback before concluding the failure is yours.

## Version numbering

Semantic versioning. `manifest.json` and `pyproject.toml` must agree — a test
asserts it, so a half-done bump fails CI rather than shipping.

Two features in one *unreleased* version is normal; fold them under one
CHANGELOG heading. Once a version is tagged and published, it is frozen: the
next work takes the next number, and the published entry keeps saying exactly
what it said. Restoring it from `origin/main` and confirming with `diff` beats
retyping it.

## Verification before tagging

```bash
pytest                                        # whole suite, no path argument
ruff check .                                  # CI pins ruff==0.16.0
python scripts/sync_vendored_btmesh.py --check
```

The vendoring check is not optional: HACS ships
`custom_components/bluetooth_mesh/` as-is, so a change under `src/btmesh/` that
is not re-synced ships a stale library to users while every test passes.
