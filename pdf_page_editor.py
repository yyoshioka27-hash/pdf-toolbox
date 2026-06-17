"""Streamlit PDF page editor.

Run with:
    streamlit run pdf_page_editor.py
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable

import fitz  # PyMuPDF
import streamlit as st


THUMBNAIL_ZOOM = 0.22
THUMBNAIL_MAX_BYTES = 350_000


@dataclass(frozen=True)
class PageRef:
    """Reference to one original PDF page.

    The edited PDF is rebuilt by inserting original pages from their source bytes,
    so page quality is preserved and pages are not rasterized into images.
    """

    source_id: str
    source_name: str
    page_index: int
    original_page_number: int


@dataclass(frozen=True)
class PdfSource:
    name: str
    data: bytes
    page_count: int


def _file_digest(data: bytes, name: str) -> str:
    digest = hashlib.sha256()
    digest.update(name.encode("utf-8", errors="ignore"))
    digest.update(data)
    return digest.hexdigest()


def _read_pdf(uploaded_file) -> tuple[str, PdfSource, list[PageRef]]:
    data = uploaded_file.getvalue()
    source_id = _file_digest(data, uploaded_file.name)

    try:
        with fitz.open(stream=data, filetype="pdf") as doc:
            if doc.needs_pass:
                raise ValueError("パスワード付きPDFは編集できません。パスワードを解除してからアップロードしてください。")
            if doc.is_encrypted:
                raise ValueError("暗号化されたPDFは編集できません。暗号化を解除してからアップロードしてください。")
            page_count = doc.page_count
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - present friendly Streamlit error for invalid PDFs
        raise ValueError("PDFを読み込めませんでした。ファイルが壊れていないか確認してください。") from exc

    if page_count == 0:
        raise ValueError("ページが含まれていないPDFは編集できません。")

    source = PdfSource(name=uploaded_file.name, data=data, page_count=page_count)
    pages = [
        PageRef(
            source_id=source_id,
            source_name=uploaded_file.name,
            page_index=index,
            original_page_number=index + 1,
        )
        for index in range(page_count)
    ]
    return source_id, source, pages


def _make_thumbnail(page: PageRef, sources: dict[str, PdfSource]) -> bytes | None:
    source = sources.get(page.source_id)
    if source is None:
        return None

    try:
        with fitz.open(stream=source.data, filetype="pdf") as doc:
            pixmap = doc.load_page(page.page_index).get_pixmap(
                matrix=fitz.Matrix(THUMBNAIL_ZOOM, THUMBNAIL_ZOOM),
                alpha=False,
            )
            png = pixmap.tobytes("png")
            if len(png) > THUMBNAIL_MAX_BYTES:
                pixmap = doc.load_page(page.page_index).get_pixmap(
                    matrix=fitz.Matrix(0.16, 0.16),
                    alpha=False,
                )
                png = pixmap.tobytes("png")
            return png
    except Exception:
        return None


@st.cache_data(show_spinner=False, max_entries=512)
def _cached_thumbnail(source_id: str, source_name: str, source_data: bytes, page_index: int) -> bytes | None:
    page = PageRef(
        source_id=source_id,
        source_name=source_name,
        page_index=page_index,
        original_page_number=page_index + 1,
    )
    return _make_thumbnail(page, {source_id: PdfSource(source_name, source_data, page_index + 1)})


def _build_pdf(pages: Iterable[PageRef], sources: dict[str, PdfSource]) -> bytes:
    output = fitz.open()
    try:
        for page in pages:
            source = sources[page.source_id]
            with fitz.open(stream=source.data, filetype="pdf") as source_doc:
                output.insert_pdf(source_doc, from_page=page.page_index, to_page=page.page_index)
        return output.tobytes(deflate=True, garbage=4)
    finally:
        output.close()


def _reset_editor(source_id: str, source: PdfSource, pages: list[PageRef]) -> None:
    st.session_state.sources = {source_id: source}
    st.session_state.pages = pages
    st.session_state.deleted_pages = set()
    st.session_state.order_text = ",".join(str(i) for i in range(1, len(pages) + 1))


def _ensure_state() -> None:
    st.session_state.setdefault("sources", {})
    st.session_state.setdefault("pages", [])
    st.session_state.setdefault("deleted_pages", set())
    st.session_state.setdefault("order_text", "")


def _move_page(index: int, direction: int) -> None:
    new_index = index + direction
    pages = st.session_state.pages
    if 0 <= index < len(pages) and 0 <= new_index < len(pages):
        pages[index], pages[new_index] = pages[new_index], pages[index]
        st.session_state.pages = pages
        st.session_state.order_text = ",".join(str(i) for i in range(1, len(pages) + 1))


def _apply_order(order_text: str) -> None:
    pages = st.session_state.pages
    try:
        order = [int(item.strip()) for item in order_text.replace("\n", ",").split(",") if item.strip()]
    except ValueError:
        st.error("ページ順は 1,2,3 のように半角数字とカンマで入力してください。")
        return

    expected = set(range(1, len(pages) + 1))
    if set(order) != expected or len(order) != len(pages):
        st.error(f"1〜{len(pages)} のページ番号を重複なくすべて入力してください。")
        return

    st.session_state.pages = [pages[number - 1] for number in order]
    st.session_state.order_text = ",".join(str(i) for i in range(1, len(pages) + 1))
    st.success("ページ順を更新しました。")


def _delete_selected() -> None:
    selected = st.session_state.deleted_pages
    if not selected:
        st.info("削除するページが選択されていません。")
        return

    st.session_state.pages = [page for index, page in enumerate(st.session_state.pages) if index not in selected]
    st.session_state.deleted_pages = set()
    st.session_state.order_text = ",".join(str(i) for i in range(1, len(st.session_state.pages) + 1))
    st.success("選択したページを削除しました。")


def _render_page_list() -> None:
    pages: list[PageRef] = st.session_state.pages
    sources: dict[str, PdfSource] = st.session_state.sources

    if not pages:
        st.info("PDFをアップロードすると、ここにページ一覧が表示されます。")
        return

    st.caption("サムネイルは表示用の低解像度画像です。ダウンロードPDFは元ページをそのまま使用します。")

    for index, page in enumerate(pages):
        with st.container(border=True):
            cols = st.columns([1.4, 2.2, 1.2, 1.2, 1.4])
            thumbnail = _cached_thumbnail(
                page.source_id,
                page.source_name,
                sources[page.source_id].data,
                page.page_index,
            )
            with cols[0]:
                if thumbnail:
                    st.image(thumbnail, caption=f"ページ {index + 1}", use_container_width=True)
                else:
                    st.warning("サムネイルを表示できません。")
            with cols[1]:
                st.markdown(f"### ページ {index + 1}")
                st.write(f"元ファイル: {page.source_name}")
                st.write(f"元ページ: {page.original_page_number}")
            with cols[2]:
                if st.button("上へ", key=f"up_{index}", disabled=index == 0):
                    _move_page(index, -1)
                    st.rerun()
            with cols[3]:
                if st.button("下へ", key=f"down_{index}", disabled=index == len(pages) - 1):
                    _move_page(index, 1)
                    st.rerun()
            with cols[4]:
                checked = st.checkbox("削除対象", key=f"delete_{index}")
                if checked:
                    st.session_state.deleted_pages.add(index)
                else:
                    st.session_state.deleted_pages.discard(index)


def main() -> None:
    st.set_page_config(page_title="PDFページ編集アプリ", layout="wide")
    _ensure_state()

    st.title("PDFページ編集アプリ")
    st.write("建築図面PDFなどのページを、画質を維持したまま追加・削除・並び替えできます。")

    st.header("PDFアップロード")
    uploaded_pdf = st.file_uploader("編集するPDFをアップロードしてください", type=["pdf"], key="main_pdf")
    if uploaded_pdf is not None and st.button("このPDFで編集を開始", type="primary"):
        try:
            source_id, source, pages = _read_pdf(uploaded_pdf)
            _reset_editor(source_id, source, pages)
            st.success(f"{source.name} を読み込みました（{source.page_count}ページ）。")
        except ValueError as exc:
            st.error(str(exc))

    st.header("ページ一覧")
    _render_page_list()

    st.header("ページ削除")
    st.write("ページ一覧の「削除対象」にチェックを入れてから削除してください。")
    st.button("選択したページを削除", on_click=_delete_selected, disabled=not st.session_state.pages)

    st.header("PDF追加")
    additional_pdf = st.file_uploader("末尾に追加するPDFをアップロードしてください", type=["pdf"], key="additional_pdf")
    if additional_pdf is not None and st.button("PDFを末尾に追加"):
        try:
            source_id, source, pages = _read_pdf(additional_pdf)
            st.session_state.sources[source_id] = source
            st.session_state.pages.extend(pages)
            st.session_state.order_text = ",".join(str(i) for i in range(1, len(st.session_state.pages) + 1))
            st.success(f"{source.name} の {source.page_count} ページを末尾に追加しました。")
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))

    st.header("ページ並び替え")
    st.write("ページ一覧の「上へ」「下へ」ボタンで移動できます。まとめて変更する場合はページ順を入力してください。")
    order_text = st.text_area(
        "ページ順（例: 1,3,2,4）",
        value=st.session_state.order_text,
        disabled=not st.session_state.pages,
        help="現在表示されているページ番号を、希望する順番で重複なく入力してください。",
    )
    if st.button("入力したページ順を適用", disabled=not st.session_state.pages):
        _apply_order(order_text)
        st.rerun()

    st.header("編集後PDFダウンロード")
    if st.session_state.pages:
        try:
            edited_pdf = _build_pdf(st.session_state.pages, st.session_state.sources)
            st.download_button(
                "編集後PDFをダウンロード",
                data=edited_pdf,
                file_name="edited_pdf.pdf",
                mime="application/pdf",
            )
            with st.expander("編集後のページ構成を確認"):
                for index, page in enumerate(st.session_state.pages, start=1):
                    st.write(f"{index}. {page.source_name} - 元ページ {page.original_page_number}")
        except Exception:
            st.error("編集後PDFの作成中にエラーが発生しました。PDFファイルを確認してください。")
    else:
        st.info("編集後PDFをダウンロードするには、まずPDFをアップロードしてください。")


if __name__ == "__main__":
    main()
