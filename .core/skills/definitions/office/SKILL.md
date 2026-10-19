---
name: office
description: How to make good Word, Excel, PowerPoint and PDF files with Lucy's office tools. Injected only while those tools are in play. Edit freely (this file is yours, Lucy does not change it).
---
OFFICE FILES: never write library code; the tools build the file and send it to the chat.
1. NEW file: docx_create / xlsx_create / pptx_create / pdf_create. CHANGE a file: docx_edit / xlsx_edit / pptx_edit. LOOK inside any file: office_read.
2. Before editing, office_read the file first and copy the exact words into find / match. Slide and table numbers start at 1.
3. Easiest input is markdown. Word / PDF: '# Title', '## Heading', paragraphs, '- bullets', '1. numbers', '| a | b |' tables. PowerPoint: one block per slide, blocks separated by a line '---', each with '# Title' and '- bullets'; 'Notes: ...' adds speaker notes.
4. Write real content, never placeholders like 'Lorem ipsum' or '[insert]'. Documents: short paragraphs under clear headings. Slides: one idea per slide, at most 6 bullets of about 12 words; details go in Notes. Use style 'resume' only for a CV.
5. Spreadsheets: put headers in 'columns', numbers as real numbers, and anything calculated as a formula ('=B2*C2', '=SUM(B2:B9)'), never a typed-in result. A percent is 0.12 with format 'percent'.
6. If the answer says 'NOT sent to the chat yet', fix the problem it names and call the same tool again with the same path.
7. To check your work, call office_render, then analyze_image on the pictures ('is any text cut off or overlapping?') and fix what looks wrong. Do this for decks and for anything the user will print or present.
8. The file is delivered automatically. Do not write the file path or a MEDIA: line yourself; just say what you made in one or two sentences.
