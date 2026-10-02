# How to export a run or a decision brief

Download a run or an ensemble as a full report (Markdown, HTML or PDF), or as a one-page decision brief.

Exports are built from what is stored; they make no model calls. Private persona fields (withheld concerns and calibration notes) are left out. Every persona and consultant name is written "(bot) Ruth", so a reader who never opened the app knows the words are simulated; an operator's injected message is not marked.

## Prerequisites

- Matrix Studio open and signed in.
- For a run: one that has ended with a transcript (**Complete**, **Stopped** or **Capped**).
- For PDF: pop-ups allowed for the app's site.

## Export a run

1. Open the run and tap **⋯** in the header.
2. Under **Export**, tap **Markdown** or **HTML** to download the report, or **PDF** to open your browser's print dialog with the HTML report; choose "Save as PDF" there.

   If the print window was blocked, the menu says so. Allow pop-ups, or download the HTML and print it.

## Get a run's decision brief

1. Open the run, tap **⋯**, and choose **Decision brief**.

   The brief opens in a window: the bottom line, what would settle it, what was assumed, the standing objections, and how far to trust it.

2. Tap **Download HTML** or **Download Markdown**.
3. Close it with **✕** or by clicking outside it.

## Export an ensemble or its brief

1. Open the ensemble (**Ensembles** in the bottom bar).
2. At the top of its page, tap **Brief** for the one-page brief, or **Markdown**, **HTML** or **PDF** beside **Export** for the full report.

   Both are available at any time. Exported before the report exists, they say which conversations are still running and that there is no report yet.

3. For one member conversation's brief, tap **Brief** beside it under **Conversations**.

## From a script

**Deployed system.** With your ID token (see [How to run an ensemble](run-an-ensemble.md), "a different working assumption", step 2):

```bash
curl -s -H "Authorization: Bearer $ID_TOKEN" "<SpaUrl>/api/runs/<run-id-or-name>/export?format=md" -o run.md
curl -s -H "Authorization: Bearer $ID_TOKEN" "<SpaUrl>/api/runs/<run-id-or-name>/brief?format=html" -o brief.html
```

The ensemble equivalents are `/api/ensembles/<ensemble-id>/export` and `/api/ensembles/<ensemble-id>/brief`. `format` is `md` or `html`.

## Check it worked

The downloaded file is named after the run or ensemble, for example `<name>-run.md` or `<name>-run-brief.html`, and opens in any browser or Markdown viewer.

## Related

- What each export contains: [reference](../reference/)
