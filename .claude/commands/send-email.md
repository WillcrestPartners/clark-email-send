# Send Email from clark@willcrestpartners.com

Helps the user compose and send an email from clark@willcrestpartners.com via the Gmail API.

## Steps

1. Ask the user: "Who should this email go to? (Enter the full email address)"
   - Store the answer as `RECIPIENT`

2. Ask the user: "What is the subject line?"
   - Store the answer as `SUBJECT`

3. Ask the user: "What is the message body? (Type it out — press Enter twice when done)"
   - Store the answer as `BODY`

4. Run the following command from the `email_tool` directory:
   ```bash
   cd email_tool && python send_email.py --to "$RECIPIENT" --subject "$SUBJECT" --body "$BODY"
   ```

5. Report back what the script printed (success message or error).

## Notes

- The script will show a preview and ask for "yes" before sending — this is intentional.
- If you see an error about `GOOGLE_CREDENTIALS_PATH`, the user needs to complete Phase 1 of WORKPLAN.md first.
- If you see a "daily limit" error, the user has hit their configured safety cap for today.
