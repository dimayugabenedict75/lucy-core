# (Expanding the existing Toolset from the previous step)

class Toolset:
    # ... (existing tools) ...

    def run_powershell_admin(self, command: str):
        """
        Executes a command in PowerShell with elevated privileges.
        Use this for system-level changes (reboot, hardware, services).
        """
        # We use 'powershell.exe' directly with the -Command flag
        # The command is wrapped in double quotes to handle complex strings
        import subprocess
        result = subprocess.run(
            ["powershell.exe", "-Command", command],
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout

    def list_drive_files(self):
        """
        List the most recent 10 files in your Google Drive.
        """
        from googleapiclient.discovery import build
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request
        import json

        # Use the new credentials
        with open(r'C:\Users\dimay\Lucy\Lucy_Core\google_client_secret.json') as f:
            creds_data = json.load(f)

        # Build a temporary credentials flow to get the token
        from google_auth_oauthlib.flow import InstalledAppFlow
        flow = InstalledAppFlow.from_client_config(creds_data, scopes=['https://www.googleapis.com/auth/drive'])
        creds = flow.run_local_server(port=0)

        service = build('drive', 'v3', credentials=creds)

        results = service.files().list(
            pageSize=10,
            q="mimeType != 'application/1024' and mimeType != 'image/'",
            fields="files(name, id, mimeType)"
        ).execute()

        files = results.get('files', [])
        if not files:
            return "No files found in your Google Drive."

        return ", ".join([f"{f['name']} ({f['id']})" for f in files])

    def create_google_doc(self, title: str):
        """
        Create a new Google Doc with the given title and return its name.
        """
        from googleapiclient.discovery import build
        from google_auth_oauthlib.flow import InstalledAppFlow
        import json

        with open(r'C:\Users\dimay\Lucy\Lucy_Core\google_client_secret.json') as f:
            creds_data = json.load(f)

        flow = InstalledAppFlow.from_client_config(creds_data, scopes=['https://www.googleapis.com/auth/drive'])
        creds = flow.run_local_server(port=0)

        service = build('docs', 'v1', credentials=creds)

        doc = service.documents().create(body={'title': title}).execute()
        return f"Created new Google Doc: {doc['name']} (ID: {doc['documentId']})"

    def get_doc_content(self, title: str):
        """
        Retrieve the full text content of a Google Doc by its title.
        """
        from googleapiclient.discovery import build
        from google_auth_oauthlib.flow import InstalledAppFlow
        import json

        with open(r'C:\Users\dimay\Lucy\Lucy_Core\google_client_secret.json') as f:
            creds_data = json.load(f)

        flow = InstalledAppFlow.from_client_config(creds_data, scopes=['https://www.googleapis.com/auth/drive'])
        creds = flow.run_local_server(port=0)

        service = build('docs', 'v1', credentials=creds)

        results = service.documents().list(q=f"title = '{title}'", fields="files(id, name)").execute()
        files = results.get('files', [])
        if not files:
            return f"Could not find a document titled '{title}'."

        doc_id = files[0]['id']
        doc = service.documents().get(documentId=doc_id).execute()

        content = ""
        content_struct = doc.get('content', [])
        for element in content_struct:
            if 'paragraph' in element.get('elements', []):
                for part in element['elements']:
                    content += part.get('textrun', {}).get('content', "")

        return content.strip()
