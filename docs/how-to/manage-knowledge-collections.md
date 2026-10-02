# How to manage knowledge collections

Create a collection of documents, add documents to it, bind it to a run or to one persona, and share it with another user.

A collection is indexed once and can be searched from any run that binds it. Text pasted into a persona in the new-run form, by contrast, belongs to that one run.

## Prerequisites

- Matrix Studio open and signed in.
- Documents as PDF, Word (`.docx`), Markdown or plain text, up to 10 MB each by default, or text you can paste.
- To share with a user: their Cognito `sub` (see step 3 of "Share a collection").

## Create a collection and add documents

1. Tap **Knowledge** in the bottom bar.
2. Type a name in **New collection name…** and tap **Create**. The new collection opens.
3. Under **Add a document**, tap **Choose file** (or drop a file on it), or paste text into the text box.

   A file is read and converted to text first; nothing is stored yet. Add files one at a time.

4. Read the note under the text box. "Extracted N characters — check it, then add." means it worked. "Extraction produced NO text" usually means a scanned PDF with no text layer; paste the text instead.
5. Correct the **Title** and the text if needed, and tap **Add and embed**.

   The document is stored and embedded in one step, which makes a small embedding call. "Added and embedded N chunks." confirms it is searchable.

6. Repeat for each document.

To remove a document, tap **remove** beside it. Its embeddings go with it. There is no way to delete a whole collection.

## Bind a collection to a run

Binding decides who in a run searches the collection. Retrieval is switched on for the run automatically.

1. In the new-run wizard, go to the **Knowledge** step.
2. Under **Knowledge bases — the whole cast**, tick the collection. Every persona may search it.
3. To give a collection to one persona only, go to the **Cast** step, open that persona's **Convictions & background documents** section, and tick the collection under **Knowledge bases — this persona only**.
4. To give a collection to a consultant, tick it in that consultant's card (see [How to add consultants](add-consultants.md)).
5. Leave **Ask personas to cite their sources inline** ticked (Knowledge step) to have each persona label the passage a sentence relies on.
6. Launch the run.

If a collection you bound is no longer shared with you when you launch, the launch is refused and names its id (shown under each collection's name in **Knowledge**).

## Share a collection

Only the owner can share, and only the owner can add or remove documents.

1. In **Knowledge**, open the collection.
2. Under **Shared with**, choose **User** or **Group**.
3. Enter the recipient: a user's Cognito `sub`, or a Cognito group name. The app does not show anyone's `sub`; an administrator can look it up:

   ```bash
   aws cognito-idp list-users --user-pool-id <user-pool-id> \
     --filter 'email = "theo@example.com"' \
     --query "Users[0].Attributes[?Name=='sub'].Value" --output text
   ```

   The value is not checked: a mistyped `sub` grants access to nobody, without an error.

4. Tap **Share**. The grant appears in the list with its kind.

To stop sharing, tap **revoke** beside the grant. It takes effect from the next turn of any run that searches the collection.

## Check it worked

- The collection's card shows its document count.
- The recipient sees the collection in their **Knowledge** tab tagged **shared with you**, and can tick it in their own new-run wizard, where it is tagged **shared**.
- In a run that bound it, tap a message to open **Message context**: **In front of them** lists the passages that persona retrieved.

## Related

- File formats, size limits and retrieval settings: [reference](../reference/)
- How bindings, grants and revocation work: [explanation](../explanation/)
