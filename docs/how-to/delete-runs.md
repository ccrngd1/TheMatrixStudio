# How to delete runs

Permanently delete an owner's runs, with their events, snapshots, summaries, documents and vectors, using `scripts/delete_runs.py`.

**Deployed system, and irreversible.** There is no undo and no delete button in the app. If you only want a run out of your list, hide it instead: [How to hide and show runs](hide-and-show-runs.md).

## Prerequisites

- The repository checked out with its Python environment installed.
- The Cognito `sub` of the owner whose runs you want to delete. Runs created by a local server belong to `local-single-user`; the deployment checks leave theirs under `phase5-verify`.
- Credentials for the account. A dry run needs `dynamodb:Scan` on the runs table and `dynamodb:Query` on the runs, events, snapshots, summaries, threads and documents tables. Deleting also needs `dynamodb:DeleteItem` on those tables, `s3:DeleteObject` on the data bucket and `s3vectors:DeleteVectors` on the vector bucket.

Know the scope before you start:

- The script deletes **every** run of each owner you name. It cannot pick out single runs.
- It does not delete ensemble records (an ensemble whose members are gone lists them as never started), aside messages, knowledge bases (including collections made by research) or avatar images.

## Steps

1. Find the owner's `sub` if you only know their email (needs `cognito-idp:ListUsers`):

   ```bash
   export AWS_REGION=<region>
   out() { aws cloudformation describe-stacks --stack-name matrix-studio-stack \
     --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
   aws cognito-idp list-users --user-pool-id "$(out UserPoolId)" \
     --filter 'email = "someone@example.com"' \
     --query "Users[0].Attributes[?Name=='sub'].Value" --output text
   ```

2. Set the storage names. Set `VECTOR_BUCKET` too: without it the script counts the vectors but does not delete them.

   ```bash
   export TABLE_PREFIX=matrix-studio
   export DATA_BUCKET=$(out DataBucketName) VECTOR_BUCKET=$(out VectorBucketName)
   ```

3. Do a dry run. Pass `--region` explicitly; the script defaults to `us-east-1` and does not read `AWS_REGION`.

   ```bash
   python scripts/delete_runs.py --owner <sub> --region "$AWS_REGION"
   ```

   Repeat `--owner` to include several owners. For every owner except some, use `--all-owners --except-owner <sub>`.

4. Read the output. It lists each run by name with what would go from each store, then totals under `WOULD DELETE`, and ends with `Nothing was deleted. Re-run with --apply.` Check that the list holds only runs you mean to lose.

5. Delete:

   ```bash
   python scripts/delete_runs.py --owner <sub> --region "$AWS_REGION" --apply
   ```

   The totals are printed under `DELETED`.

## Check it worked

Run the dry run from step 3 again. It prints `Nothing matched. Refusing to report success for a no-op.` and exits with status 1, and the runs no longer appear in that user's Runs screen.

## Related

- What a run's data consists of, store by store: [reference](../reference/)
- Why deletion is a script rather than a route: [explanation](../explanation/)
