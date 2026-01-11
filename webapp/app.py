import logging

import pandas as pd
import streamlit as st
from monopoly.generic.generic import GenericParserError
from monopoly.pdf import MissingPasswordError, PdfDocument
from streamlit.runtime.uploaded_file_manager import UploadedFile

from webapp.constants import APP_DESCRIPTION
from webapp.helpers import create_df, parse_bank_statement, show_df
from webapp.logo import logo
from webapp.models import ProcessedFile, TransactionMetadata

# number of files that need to be added before progress bar appears
PBAR_MIN_FILES = 4

logger = logging.getLogger(__name__)


def app() -> pd.DataFrame:
    st.set_page_config(page_title="Statement Sensei", layout="wide")
    st.image(logo, width=450)
    st.markdown(APP_DESCRIPTION)

    files = get_files()

    df = None
    if files:
        st.session_state.pop("df", None)

    if "df" in st.session_state and not files:
        df = st.session_state["df"]

    if files:
        processed_files = process_files(files)

        if processed_files:
            df = create_df(processed_files)

    if df is not None:
        show_df(df)

    return df


def process_files(uploaded_files: list[UploadedFile]) -> list[ProcessedFile] | None:
    num_files = len(uploaded_files)
    show_pbar = num_files > PBAR_MIN_FILES

    pbar = st.progress(0, text="Processing PDFs") if show_pbar else None

    processed_files: list[ProcessedFile] = []
    skipped_files = 0
    for i, file in enumerate(uploaded_files):
        if pbar:
            pbar.progress(i / num_files, text=f"Processing {file.name}")

        try:
            file_bytes = file.getvalue()
            document = PdfDocument(file_bytes=file_bytes)
            document._name = file.name
        except Exception:
            logger.exception("Failed to load uploaded PDF %s", getattr(file, "name", "<unknown>"))
            st.error(f"Couldn't read {file.name} as a PDF.")
            skipped_files += 1
            continue

        # attempt to use passwords stored in environment to unlock
        # if no passwords in environment, then ask user for password
        if document.is_encrypted:  # pylint: disable=no-member
            try:
                document = document.unlock_document()

            except MissingPasswordError:
                document = handle_encrypted_document(document)

        if document:
            processed_file = handle_file(document, file_bytes)
            if processed_file is not None:
                processed_files.append(processed_file)
            else:
                skipped_files += 1

    if pbar:
        pbar.empty()

    if skipped_files:
        st.warning(f"Skipped {skipped_files} file(s) due to parsing errors.")

    return processed_files


def handle_file(document: PdfDocument, file_bytes: bytes) -> ProcessedFile | None:
    cache_key: str | None
    try:
        document_id = document.xref_get_key(-1, "ID")[-1]
        cache_key = document.name + document_id
    except Exception:
        cache_key = None

    if cache_key and cache_key in st.session_state:
        return st.session_state[cache_key]

    try:
        processed_file = parse_bank_statement(document)
    except GenericParserError:
        logger.exception("Generic parser failed for %s", document.name)
        from webapp.fallback_parsers.hlb import try_parse_hlb_primebiz_current_account

        if transactions := try_parse_hlb_primebiz_current_account(file_bytes):
            processed_file = ProcessedFile(transactions, TransactionMetadata(bank_name="HongLeongBank"))
            if cache_key:
                st.session_state[cache_key] = processed_file
            return processed_file

        st.error(
            f"Couldn't parse {document.name}. This statement format isn't supported yet.",
        )
        return None
    except Exception:
        logger.exception("Failed to parse bank statement for %s", document.name)
        st.error(f"Couldn't parse {document.name}.")
        return None

    if cache_key:
        st.session_state[cache_key] = processed_file
    return processed_file


def handle_encrypted_document(document: PdfDocument) -> PdfDocument | None:
    passwords: list[str] = st.session_state.setdefault("pdf_passwords", [])

    # Try existing passwords first
    for password in passwords:
        document.authenticate(password)
        if not document.is_encrypted:  # pylint: disable=no-member
            return document

    # Prompt user for password if none of the existing passwords work
    password_container = st.empty()
    password = password_container.text_input(
        label="Password",
        type="password",
        placeholder=f"Enter password for {document.name}",
        key=document.name,
    )

    if not password:
        return None

    document.authenticate(password)

    if not document.is_encrypted:  # pylint: disable=no-member
        passwords.append(password)
        password_container.empty()
        return document

    st.error("Wrong password. Please try again.")
    return None


def get_files() -> list[UploadedFile]:
    return st.file_uploader(
        label="Upload a bank statement",
        type="pdf",
        label_visibility="hidden",
        accept_multiple_files=True,
    )


if __name__ == "__main__":
    app()
