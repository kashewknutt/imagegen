from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from pathlib import Path

import streamlit as st
from PIL import Image

from src.config import load_config
from src.genai_client import GenAiImageClient
from src.pipeline import (
    generate_pair,
    write_missing_report,
    approve_many,
    skip,
    load_entries_and_state,
    prepare_work_item_for_path,
    prepare_work_item_for_paths,
)
from src.folder_ingest import iter_groups
from src.name_group import base_key_from_path
from src.cost_log import append_cost_row, make_generate_row, estimate_cost_usd, extract_image_modality_tokens
from src.xlsx_ingest import iter_rows as xlsx_iter_rows, index_by_sku as xlsx_index_by_sku, list_sheets as xlsx_list_sheets

from src.image_resolve import SUPPORTED_EXTS
from src.lease import try_acquire_lease, list_active_leases

log = logging.getLogger(__name__)


def _load_image(path: Path) -> Image.Image:
    return Image.open(path).convert("RGB")


def _safe_open_image(path: Path) -> Image.Image | None:
    try:
        return Image.open(path).convert("RGB")
    except Exception:
        return None


@contextmanager
def _bordered_container():
    try:
        with st.container(border=True):
            yield
    except TypeError:
        with st.container():
            yield


def _read_state(path: Path) -> dict:
    try:
        import json

        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _list_candidates_for_key(images_dir: Path, key: str) -> list[Path]:
    if not images_dir.exists():
        return []
    out: list[Path] = []
    for p in sorted(images_dir.iterdir()):
        if not p.is_file():
            continue
        if p.suffix.lower() not in SUPPORTED_EXTS:
            continue
        if base_key_from_path(p) == key:
            out.append(p)
    return out


def _render_gallery(cfg) -> None:
    st.subheader("Gallery (Approved)")
    from src.state_store import StateStore

    store = StateStore(cfg.state_path)
    state = _read_state(cfg.state_path)
    skus = state.get("skus") or {}
    rows = [v for v in skus.values() if isinstance(v, dict) and v.get("status") == "approved"]
    if not rows:
        st.info("No approved entries yet.")
        return

    rows.sort(key=lambda r: str(r.get("sku") or ""))
    per_page = st.selectbox("Items per page", [5, 10, 20], index=1, key="gallery_per_page")
    max_page = max(1, (len(rows) + per_page - 1) // per_page)
    page = st.number_input("Page", min_value=1, max_value=max_page, value=1, key="gallery_page")
    start = (page - 1) * per_page
    page_rows = rows[start : start + per_page]

    for rec in page_rows:
        sku = str(rec.get("sku") or "")
        version = int(rec.get("approved_version") or 0)
        refs = rec.get("selected_reference_paths") or []
        if not isinstance(refs, list) or not refs:
            st.markdown(f"### `{sku}` (v{version})")
            st.warning("No reference paths recorded.")
            continue

        with _bordered_container():
            header_cols = st.columns([3, 1.2, 1.2])
            with header_cols[0]:
                st.markdown(f"### `{sku}` (v{version})")
            with header_cols[1]:
                if st.button(
                    f"Open",
                    key=f"gallery_open::{sku}",
                    width="stretch",
                    type="primary",
                ):
                    st.session_state.pending_nav = "Generate"
                    st.session_state.override_key = sku
                    st.session_state.generated = {}
                    st.session_state.key = ""
                    st.rerun()
            with header_cols[2]:
                if st.button(
                    f"Needs Rework",
                    key=f"gallery_rework::{sku}",
                    width="stretch",
                ):
                    st_rec = store.get(sku)
                    store.update(
                        sku,
                        status="pending",
                        attempts=int(st_rec.attempts or 0) + 1,
                        last_temp_p1="",
                        last_temp_p2="",
                        last_error="",
                    )
                    st.session_state.pending_nav = "Generate"
                    st.session_state.override_key = sku
                    st.session_state.generated = {}
                    st.session_state.key = ""
                    st.rerun()
            for ref_path_str in refs:
                ref_path = Path(str(ref_path_str))
                out_dir = cfg.outputs_dir / sku
                p1 = out_dir / f"prompt1_v{version}.{cfg.output_format}"
                p2 = out_dir / f"prompt2_v{version}.{cfg.output_format}"

                c1, c2, c3 = st.columns(3)
                with c1:
                    st.caption(f"Original: {ref_path.name}")
                    img = _safe_open_image(ref_path)
                    if img is None:
                        st.warning("Missing/unreadable original")
                    else:
                        st.image(img, width="stretch")
                with c2:
                    st.caption("Prompt 1")
                    if p1.exists():
                        st.image(_load_image(p1), width="stretch")
                    else:
                        st.warning("Missing prompt1 output")
                with c3:
                    st.caption("Prompt 2")
                    if p2.exists():
                        st.image(_load_image(p2), width="stretch")
                    else:
                        st.warning("Missing prompt2 output")


def _render_costs(cfg) -> None:
    st.subheader("Costs / Calls")
    try:
        import pandas as pd
    except Exception:
        st.error("Missing pandas dependency; reinstall requirements.")
        return

    if not cfg.cost_log_csv.exists():
        st.info(f"No cost log yet: {cfg.cost_log_csv}")
        return

    df = pd.read_csv(cfg.cost_log_csv)
    if df.empty:
        st.info("Cost log is empty.")
        return

    total = len(df)
    success = int((df.get("status") == "success").sum()) if "status" in df.columns else 0
    error = int((df.get("status") == "error").sum()) if "status" in df.columns else 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Rows logged", total)
    c2.metric("Success", success)
    c3.metric("Errors", error)
    c4.metric("Success rate", f"{(success / max(1,total))*100:.1f}%")

    if "estimated_cost_usd" in df.columns:
        df["estimated_cost_usd_num"] = pd.to_numeric(df["estimated_cost_usd"], errors="coerce").fillna(0.0)
        st.metric("Total estimated cost (USD)", f"{float(df['estimated_cost_usd_num'].sum()):.4f}")
        nz = df[df["estimated_cost_usd_num"] > 0]
        st.metric("Average cost (non-zero rows)", f"{float(nz['estimated_cost_usd_num'].mean() or 0.0):.6f}")

        if {"action", "status"}.issubset(df.columns):
            gen = df[(df["action"] == "generate") & (df["status"] == "success")].copy()
            st.subheader("Last 10 successful calls")
            st.dataframe(gen.tail(10), width="stretch")

        if "ts_utc" in df.columns:
            df["ts"] = pd.to_datetime(df["ts_utc"], errors="coerce")
            st.subheader("Cost over time")
            st.line_chart(df.sort_values("ts").set_index("ts")["estimated_cost_usd_num"], height=220)

        if "model" in df.columns:
            st.subheader("Cost by model")
            by_model = df.groupby("model")["estimated_cost_usd_num"].sum().sort_values(ascending=False)
            st.bar_chart(by_model, height=220)
    else:
        st.info("Cost estimates not enabled. Fill `pricing_usd_per_million_tokens` in config.yaml to estimate USD costs.")

    st.subheader("Raw log")
    st.dataframe(df.tail(200), width="stretch")


def _render_generate(cfg) -> None:
    store = None
    ordered_keys: list[str] = []
    key_to_images: dict[str, list[Path]] = {}
    sku_to_xlsx_row: dict[str, object] = {}
    xlsx_sheet_row_counts: dict[str, int] = {}
    xlsx_total_rows_all_sheets: int | None = None
    xlsx_all_sheets: list[str] = []

    if cfg.input_mode == "folder":
        groups = iter_groups(cfg.images_dir)
        ordered_keys = [g.key for g in groups]
        key_to_images = {g.key: g.images for g in groups}
        from src.state_store import StateStore

        store = StateStore(cfg.state_path)
        store.ensure_skus(ordered_keys)
    else:
        if cfg.input_mode == "xlsx":
            @st.cache_data(show_spinner=False)
            def _cached_xlsx_counts(xlsx_path_str: str) -> tuple[list[str], dict[str, int], int]:
                xlsx_path = Path(xlsx_path_str)
                sheets = xlsx_list_sheets(xlsx_path)
                counts: dict[str, int] = {}
                for sh in sheets:
                    counts[sh] = len(xlsx_iter_rows(xlsx_path, [sh]))
                return sheets, counts, sum(counts.values())

            try:
                xlsx_all_sheets, xlsx_sheet_row_counts, xlsx_total_rows_all_sheets = _cached_xlsx_counts(str(cfg.xlsx_path))
            except Exception:
                xlsx_all_sheets, xlsx_sheet_row_counts, xlsx_total_rows_all_sheets = [], {}, None

            xrows = xlsx_iter_rows(cfg.xlsx_path, cfg.xlsx_sheets)
            sku_map = xlsx_index_by_sku(xrows, sku_column="SKU")
            ordered_keys = list(sku_map.keys())
            sku_to_xlsx_row = sku_map
            from src.state_store import StateStore

            store = StateStore(cfg.state_path)
            store.ensure_skus(ordered_keys)
            for sku in ordered_keys:
                key_to_images[sku] = _list_candidates_for_key(cfg.images_dir, sku)
        else:
            entries, store = load_entries_and_state(cfg)
            ordered_keys = [e.sku for e in entries]
            for sku in ordered_keys:
                key_to_images[sku] = _list_candidates_for_key(cfg.images_dir, sku)

    if store is None:
        st.error("State store not initialized.")
        return

    if "session_id" not in st.session_state:
        st.session_state.session_id = uuid.uuid4().hex

    leases_dir = cfg.outputs_dir / "_leases"
    max_parallel = int(getattr(cfg, "max_parallel_sessions", 4) or 4)
    lease_ttl = int(getattr(cfg, "lease_ttl_seconds", 3600) or 3600)

    override_key = st.session_state.get("override_key")
    next_key: str | None = None

    # Refresh current lease heartbeat if we still hold it.
    cur_lease_key = st.session_state.get("lease_key")
    if cur_lease_key:
        p = leases_dir / f"{cur_lease_key}.json"
        if p.exists():
            try:
                import json

                data = json.loads(p.read_text(encoding="utf-8"))
                if str(data.get("session_id") or "") == str(st.session_state.session_id):
                    p.touch(exist_ok=True)
            except Exception:
                pass

    def _acquire_or_none(key: str) -> bool:
        lease = try_acquire_lease(
            leases_dir,
            key,
            str(st.session_state.session_id),
            ttl_seconds=lease_ttl,
            max_concurrent=max_parallel,
        )
        if lease is None:
            return False
        # If switching keys, release previous lease.
        prev = st.session_state.get("lease_key")
        if prev and prev != key:
            prev_path = leases_dir / f"{prev}.json"
            try:
                prev_path.unlink(missing_ok=True)  # type: ignore[arg-type]
            except Exception:
                pass
        st.session_state.lease_key = key
        return True

    if override_key and override_key in ordered_keys:
        if not _acquire_or_none(str(override_key)):
            st.warning(f"`{override_key}` is currently being worked on in another window. Try again later.")
            active = list_active_leases(leases_dir, lease_ttl)
            st.caption(f"Active leases: {len(active)}/{max_parallel}")
            st.stop()
        next_key = str(override_key)
    else:
        # Prefer resuming pending items with attempts>0, but skip anything leased by another session.
        actionable: list[str] = []
        for sku in ordered_keys:
            rec = store.get(sku)
            if rec.status == "pending" and int(rec.attempts or 0) > 0:
                actionable.append(sku)
        for sku in ordered_keys:
            rec = store.get(sku)
            if rec.status == "pending" and int(rec.attempts or 0) <= 0:
                actionable.append(sku)

        for sku in actionable:
            if _acquire_or_none(sku):
                next_key = sku
                break

    if not next_key:
        active = list_active_leases(leases_dir, lease_ttl)
        if max_parallel > 0 and len(active) >= max_parallel:
            st.info(f"All {max_parallel} parallel slots are currently in use. Close a window or wait for leases to expire.")
            st.caption(f"Active leases: {len(active)}/{max_parallel}")
            st.stop()
        st.success("All SKUs are approved or skipped.")
        return

    # Progress bar (overall workbook scope when using xlsx; otherwise within input list)
    approved = 0
    skipped = 0
    pending = 0
    try:
        for k in ordered_keys:
            s = store.get(k).status
            if s == "approved":
                approved += 1
            elif s == "skipped":
                skipped += 1
            else:
                pending += 1
    except Exception:
        pass
    total = max(1, len(ordered_keys))
    done = approved + skipped
    st.progress(done / total, text=f"Progress: {done}/{total} done (approved: {approved}, skipped: {skipped}, pending: {pending})")

    if st.session_state.get("override_key") == next_key:
        st.info(f"Manual selection: showing `{next_key}` from Gallery.")

    st.caption(f"Key: `{next_key}`")
    if cfg.input_mode == "xlsx" and next_key in sku_to_xlsx_row:
        xr = sku_to_xlsx_row[next_key]
        try:
            values = xr.values  # type: ignore[attr-defined]
            st.subheader("Stock.xlsx row")
            st.caption(f"Sheet: {xr.sheet} | Row: {xr.row_index_1based}")  # type: ignore[attr-defined]
            st.dataframe([values], width="stretch")
        except Exception:
            pass

    if "client" not in st.session_state:
        # Default model for new tabs/sessions.
        st.session_state.selected_model = "models/gemini-3.1-flash-image-preview"
        st.session_state.client = GenAiImageClient(
            st.session_state.selected_model,
            cfg.min_seconds_between_requests,
            semaphore_dir=str(cfg.outputs_dir / "_semaphore"),
            max_inflight_generations=int(getattr(cfg, "max_inflight_generations", 4) or 4),
        )
        st.session_state.client_info = {
            "mode": getattr(st.session_state.client, "mode", "unknown"),
            "model": st.session_state.selected_model,
        }

    with st.sidebar:
        st.header("Run Info")
        st.json(st.session_state.get("client_info", {"model": cfg.model}))

        if "available_models" not in st.session_state:
            with st.spinner("Loading available models..."):
                st.session_state.available_models = st.session_state.client.list_models()

        if st.button("Refresh models", width="stretch"):
            with st.spinner("Refreshing..."):
                st.session_state.available_models = st.session_state.client.list_models()

        models_list = st.session_state.get("available_models") or []
        if models_list and isinstance(models_list, list) and isinstance(models_list[0], dict) and models_list[0].get("error"):
            st.error(f"Model listing failed: {models_list[0].get('error')}")
        image_models = [
            m["name"]
            for m in models_list
            if isinstance(m, dict)
            and isinstance(m.get("name"), str)
            and isinstance(m.get("supported_actions"), list)
            and "generateContent" in m.get("supported_actions")
            and (
                ("image" in m["name"].lower())
                or ("image" in str(m.get("display_name", "")).lower())
                or ("image" in str(m.get("description", "")).lower())
                or ("nano banana" in str(m.get("display_name", "")).lower())
            )
        ]
        image_models = sorted(set(image_models))

        st.subheader("Model")
        if image_models:
            default_idx = image_models.index(st.session_state.selected_model) if st.session_state.selected_model in image_models else 0
            chosen = st.selectbox("Select model for generation", options=image_models, index=default_idx)
            if chosen != st.session_state.selected_model:
                st.session_state.selected_model = chosen
                st.session_state.client = GenAiImageClient(
                    st.session_state.selected_model,
                    cfg.min_seconds_between_requests,
                    semaphore_dir=str(cfg.outputs_dir / "_semaphore"),
                    max_inflight_generations=int(getattr(cfg, "max_inflight_generations", 4) or 4),
                )
                st.session_state.client_info = {"mode": "devapi", "model": st.session_state.selected_model}
                st.success(f"Using model: {st.session_state.selected_model}")
        else:
            st.warning("No image-capable models returned from listing; using config.yaml model.")
            st.caption("If this persists, check your API key, network, or try Refresh models.")
            with st.expander("Model listing (raw)", expanded=False):
                st.json(models_list)

        st.divider()
        st.subheader("Progress")
        st.caption(f"Approved: {approved} | Skipped: {skipped} | Pending: {pending} | Total: {len(ordered_keys)}")
        if cfg.input_mode == "xlsx":
            pages = len(xlsx_all_sheets) if xlsx_all_sheets else len(getattr(cfg, "xlsx_sheets", []) or [])
            st.caption(f"XLSX pages (sheets): {pages}")
            if xlsx_total_rows_all_sheets is not None:
                st.caption(f"Total rows (all pages): {xlsx_total_rows_all_sheets}")
            # Current page stats
            try:
                xr = sku_to_xlsx_row.get(next_key)
                sheet = getattr(xr, "sheet", "") if xr is not None else ""
                if sheet:
                    st.caption(f"Current page: {sheet}")
                    if sheet in xlsx_sheet_row_counts:
                        st.caption(f"Rows in current page: {xlsx_sheet_row_counts[sheet]}")
            except Exception:
                pass

    if "attempt" not in st.session_state or st.session_state.get("key") != next_key:
        st.session_state.key = next_key
        st.session_state.attempt = int(store.get(next_key).attempts) + 1
        st.session_state.generated = {}

    # If the current SKU is pending but already has generated temp outputs, reload them so the user can approve.
    if not st.session_state.generated:
        st_rec = store.get(next_key)
        if st_rec.status == "pending" and int(st_rec.attempts or 0) > 0 and st_rec.selected_reference_paths:
            attempt = int(st_rec.attempts)
            loaded = {}
            for ref_path_str in (st_rec.selected_reference_paths or []):
                ref_path = Path(str(ref_path_str))
                temp_dir = cfg.outputs_dir / "_temp" / next_key
                p1 = temp_dir / f"prompt1_attempt{attempt}.{cfg.output_format}"
                p2 = temp_dir / f"prompt2_attempt{attempt}.{cfg.output_format}"
                if p1.exists() and p2.exists():
                    loaded[next_key] = {
                        "ref_paths": [str(Path(x)) for x in (st_rec.selected_reference_paths or [])],
                        "ref_path": str(ref_path),
                        "p1": str(p1),
                        "p2": str(p2),
                        "meta": {"attempt": attempt, "resumed": True},
                    }
            if loaded:
                st.session_state.generated = loaded
                st.session_state.attempt = attempt

    candidates = key_to_images.get(next_key) or []
    if not candidates:
        st.error("No images found for this key. Marking as skipped.")
        skip(store, next_key, "no_images_for_key")
        st.rerun()
        return

    st.subheader("Select reference images for generation")
    st.caption("Select one or more. The first selected image is treated as the PRIMARY reference for fidelity checks.")

    sel_key = f"selected_refs::{next_key}"
    if sel_key not in st.session_state:
        st.session_state[sel_key] = [str(candidates[0])]

    st.write("Select images (you can pick multiple):")
    cols = st.columns(min(4, len(candidates)))
    for i, p in enumerate(candidates):
        with cols[i % len(cols)]:
            img = _safe_open_image(p)
            if img is None:
                st.warning(p.name)
                st.caption("Unreadable image")
            else:
                st.image(img, caption=p.name, width="stretch")
            selected_list = list(st.session_state.get(sel_key) or [])
            is_selected = str(p) in selected_list
            btn_label = "Selected" if is_selected else "Select"
            if st.button(btn_label, key=f"pick::{next_key}::{p.name}", width="stretch"):
                if is_selected:
                    selected_list = [x for x in selected_list if x != str(p)]
                else:
                    selected_list.append(str(p))
                # Ensure at least one selection (fallback to first candidate)
                if not selected_list:
                    selected_list = [str(candidates[0])]
                st.session_state[sel_key] = selected_list
                st.rerun()

    selected_refs = [Path(p) for p in (st.session_state.get(sel_key) or [])]
    selected_refs = [p for p in selected_refs if p.exists()]
    if not selected_refs:
        selected_refs = [candidates[0]]
        st.session_state[sel_key] = [str(candidates[0])]
    st.caption("Current selection (order matters): " + ", ".join([f"`{p.name}`" for p in selected_refs]))

    extra_ctx = ""
    if cfg.input_mode == "xlsx" and next_key in sku_to_xlsx_row:
        try:
            row_vals = sku_to_xlsx_row[next_key].values  # type: ignore[attr-defined]
            fields = [
                ("category", row_vals.get("category")),
                ("subCategory", row_vals.get("subCategory")),
                ("metalType", row_vals.get("metalType")),
                ("metalColor", row_vals.get("metalColor")),
                ("madeFor", row_vals.get("madeFor")),
            ]
            extra_ctx = "\n".join([f"- {k}: {v}" for k, v in fields if str(v).strip() not in {"", "None"}])
        except Exception:
            extra_ctx = ""

    st.subheader("Product context (editable for this run)")
    st.caption("This does not edit your Stock.xlsx. It is only used as context for the current generation and resets for the next SKU.")
    ctx_key = f"product_ctx::{next_key}"
    # Populate with XLSX-derived context by default for this SKU, but allow edits during this session.
    if (ctx_key not in st.session_state) or (not str(st.session_state.get(ctx_key) or "").strip()):
        st.session_state[ctx_key] = extra_ctx if extra_ctx.strip() else ""
    editable_ctx = st.text_area("Product context", height=160, key=ctx_key)
    st.caption("If any context conflicts with the reference image (especially metal color/finish), the model must follow the reference image.")

    if st.button("Generate now", type="primary", width="stretch"):
        st.session_state.generated = {}
        error_placeholder = st.empty()
        with st.spinner("Generating..."):
            try:
                ref_paths = [Path(p) for p in selected_refs]
                ref_tag = ref_paths[0].stem
                work = prepare_work_item_for_paths(cfg, next_key, ref_paths)
                p1, p2, meta = generate_pair(
                    cfg,
                    st.session_state.client,
                    work,
                    st.session_state.attempt,
                    ref_tag=ref_tag,
                    extra_context=editable_ctx,
                )
                st.session_state.generated[next_key] = {
                    "ref_paths": [str(p) for p in ref_paths],
                    "ref_path": str(ref_paths[0]),
                    "p1": str(p1),
                    "p2": str(p2),
                    "meta": meta,
                }

                pm = meta.get("prompt_meta") or {}
                for prompt_id, bucket_key in [("prompt1", "p1"), ("prompt2", "p2")]:
                    m = pm.get(bucket_key) or {}
                    usage = (m.get("usage_metadata") or None) if isinstance(m, dict) else None
                    response_id = str(m.get("response_id") or "")
                    model_version = str(m.get("model_version") or "")
                    prompt_tokens = int((usage or {}).get("prompt_token_count") or 0) if isinstance(usage, dict) else 0
                    cand_tokens = int((usage or {}).get("candidates_token_count") or 0) if isinstance(usage, dict) else 0
                    img_prompt_tokens = int((m.get("image_prompt_tokens") or 0) if isinstance(m, dict) else 0)
                    img_cand_tokens = int((m.get("image_candidates_tokens") or 0) if isinstance(m, dict) else 0)
                    if isinstance(usage, dict):
                        ip, ic = extract_image_modality_tokens(usage)
                        img_prompt_tokens, img_cand_tokens = ip, ic
                    est = estimate_cost_usd(
                        cfg.pricing_usd_per_million_tokens,
                        str(getattr(st.session_state.client, "model", "")),
                        prompt_tokens,
                        cand_tokens,
                        image_prompt_tokens=img_prompt_tokens,
                        image_candidates_tokens=img_cand_tokens,
                    )
                    row = make_generate_row(
                        key=next_key,
                        ref_tag=ref_tag,
                        prompt_id=prompt_id,
                        attempt=int(st.session_state.attempt),
                        model=str(getattr(st.session_state.client, "model", cfg.model)),
                        mode="devapi",
                        status="success",
                        action="generate",
                        usage_metadata=usage if isinstance(usage, dict) else None,
                        response_id=response_id,
                        model_version=model_version,
                        estimated_cost_usd=est,
                        error="",
                    )
                    append_cost_row(cfg.cost_log_csv, row)
            except Exception as e:
                msg = str(e)
                log.exception("Generation failed for key=%s attempt=%s: %s", next_key, st.session_state.get("attempt"), msg)
                store.update(next_key, last_error=msg)
                st.session_state.generated = {}
                error_placeholder.error("Generation failed.")
                if hasattr(st.session_state, "client") and getattr(st.session_state.client, "last_raw_response", None) is not None:
                    with st.expander("Raw model response (debug)", expanded=True):
                        st.json(getattr(st.session_state.client, "last_raw_response"))
                with st.expander("Exception details", expanded=True):
                    st.write(f"Type: `{type(e).__name__}`")
                    st.exception(e)
                error_placeholder.code(msg)
                try:
                    row = make_generate_row(
                        key=next_key,
                        ref_tag=ref_tag if "ref_tag" in locals() else "",
                        prompt_id="unknown",
                        attempt=int(st.session_state.attempt),
                        model=str(getattr(st.session_state.client, "model", cfg.model)),
                        mode="devapi",
                        status="error",
                        action="generate",
                        usage_metadata=None,
                        response_id="",
                        model_version="",
                        estimated_cost_usd="",
                        error=msg,
                    )
                    append_cost_row(cfg.cost_log_csv, row)
                except Exception:
                    pass
                return

        store.update(next_key, attempts=st.session_state.attempt, selected_reference_paths=[str(p) for p in selected_refs], last_error="")
        st.rerun()

    if not st.session_state.generated:
        st.stop()

    st.subheader("Review generated outputs")
    for ref_tag, item in st.session_state.generated.items():
        ref_paths = item.get("ref_paths") or [item.get("ref_path")]
        ref_paths = [Path(str(p)) for p in ref_paths if p]
        st.markdown("### References")
        if ref_paths:
            ref_cols = st.columns(min(4, len(ref_paths)))
            for i, rp in enumerate(ref_paths):
                with ref_cols[i % len(ref_cols)]:
                    st.caption("PRIMARY" if i == 0 else "Context")
                    img = _safe_open_image(rp)
                    if img is None:
                        st.warning(rp.name)
                    else:
                        st.image(img, caption=rp.name, width="stretch")
        c2, c3 = st.columns(2)
        with c2:
            p1p = Path(item["p1"])
            if not p1p.exists():
                st.warning(f"Missing temp output: {p1p.name}")
            else:
                st.image(_load_image(p1p), width="stretch")
        with c3:
            p2p = Path(item["p2"])
            if not p2p.exists():
                st.warning(f"Missing temp output: {p2p.name}")
            else:
                st.image(_load_image(p2p), width="stretch")
        with st.expander(f"Metrics: {ref_tag}", expanded=False):
            st.json(item.get("meta", {}))

    c1, c2, c3 = st.columns([1, 1, 2])
    with c1:
        if st.button("Approve & Next", type="primary", width="stretch"):
            ref_to_temp = {ref_tag: (Path(item["p1"]), Path(item["p2"])) for ref_tag, item in st.session_state.generated.items()}
            approve_many(cfg, store, next_key, ref_to_temp)
            # Clear in-memory generated state so we never re-show this key after approval.
            st.session_state.generated = {}
            st.session_state.key = ""
            if st.session_state.get("lease_key") == next_key:
                try:
                    (cfg.outputs_dir / "_leases" / f"{next_key}.json").unlink(missing_ok=True)  # type: ignore[arg-type]
                except Exception:
                    pass
                st.session_state.lease_key = ""
            if st.session_state.get("override_key") == next_key:
                st.session_state.override_key = None
            try:
                append_cost_row(
                    cfg.cost_log_csv,
                    make_generate_row(
                        key=next_key,
                        ref_tag="",
                        prompt_id="",
                        attempt=int(st.session_state.attempt),
                        model=str(getattr(st.session_state.client, "model", cfg.model)),
                        mode="devapi",
                        status="success",
                        action="approve",
                        usage_metadata=None,
                        error="",
                    ),
                )
            except Exception:
                pass
            st.success("Saved and marked approved.")
            st.rerun()
    with c2:
        if st.button("Needs Rework (regenerate)", width="stretch"):
            try:
                append_cost_row(
                    cfg.cost_log_csv,
                    make_generate_row(
                        key=next_key,
                        ref_tag="",
                        prompt_id="",
                        attempt=int(st.session_state.attempt),
                        model=str(getattr(st.session_state.client, "model", cfg.model)),
                        mode="devapi",
                        status="success",
                        action="regenerate_click",
                        usage_metadata=None,
                        error="",
                    ),
                )
            except Exception:
                pass
            st.session_state.attempt += 1
            st.session_state.generated = {}
            st.rerun()
    with c3:
        if st.button("Skip SKU", width="stretch"):
            skip(store, next_key, "user_skipped")
            try:
                append_cost_row(
                    cfg.cost_log_csv,
                    make_generate_row(
                        key=next_key,
                        ref_tag="",
                        prompt_id="",
                        attempt=int(st.session_state.attempt),
                        model=str(getattr(st.session_state.client, "model", cfg.model)),
                        mode="devapi",
                        status="success",
                        action="skip",
                        usage_metadata=None,
                        error="user_skipped",
                    ),
                )
            except Exception:
                pass
            if st.session_state.get("lease_key") == next_key:
                try:
                    (cfg.outputs_dir / "_leases" / f"{next_key}.json").unlink(missing_ok=True)  # type: ignore[arg-type]
                except Exception:
                    pass
                st.session_state.lease_key = ""
            if st.session_state.get("override_key") == next_key:
                st.session_state.override_key = None
            st.rerun()


def main() -> None:
    cfg = load_config()
    st.set_page_config(page_title=cfg.page_title, layout="wide")
    st.title(cfg.page_title)

    if "nav" not in st.session_state:
        st.session_state.nav = "Generate"
    if "view" not in st.session_state:
        st.session_state.view = st.session_state.nav
    if "pending_nav" in st.session_state:
        st.session_state.nav = st.session_state.pending_nav
        del st.session_state["pending_nav"]

    with st.sidebar:
        st.subheader("View")
        st.radio(
            "Navigation",
            options=["Generate", "Gallery", "Costs"],
            index=["Generate", "Gallery", "Costs"].index(st.session_state.nav) if st.session_state.nav in {"Generate", "Gallery", "Costs"} else 0,
            label_visibility="collapsed",
            key="nav",
        )
        st.session_state.view = st.session_state.nav

    if st.session_state.view == "Gallery":
        _render_gallery(cfg)
    elif st.session_state.view == "Costs":
        _render_costs(cfg)
    else:
        _render_generate(cfg)


if __name__ == "__main__":
    main()
