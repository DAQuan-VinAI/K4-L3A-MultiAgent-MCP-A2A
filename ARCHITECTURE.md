# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Workflow tất định (không dùng LLM), cài đặt tại `src/student_agent/workflow.py`. Mỗi case chạy trong một MCP session riêng (`cli.py`).

```text
inputs/<case_id>.json
      │
      ▼
Coordinator ──task_assigned──► Order agent ── get_order ──► xác lập cửa sổ thời gian theo order row
      │                         │  get_order_items, get_sellers
      ├──task_assigned──► Payment agent ── get_payment_timeline, get_refund_timeline
      ├──task_assigned──► Shipment agent ── get_shipment_summary
      ├──task_assigned──► Policy agent ── get_policy
      │   ◄──handoff (decision_code + evidence_refs)── mỗi specialist
      ▼
Coordinator classify() ──handoff──► Policy agent (policy_decided: remedy theo rule)
      │
      ▼
Policy agent ──handoff──► Verifier (verification_completed) ──► outputs/<case_id>.json
                                                              └► traces/trace.jsonl
```

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | Case input | Giao task, phân loại `primary_issue` từ facts đã lọc, lắp output | `task_assigned`, `handoff` → policy-agent |
| Order/item | `claimed_order_id` | Lấy order row chuẩn, đặt cửa sổ thời gian theo domain, lọc item/seller | `handoff` `ORDER_LOADED` / `ITEMS_RESOLVED` |
| Payment | order_id + cửa sổ | Lọc capture/reconciliation/refund event, tính tổng tiền, payment refs | `handoff` `PAYMENTS_RECONCILED` / `RECONCILIATION_MISMATCH` / `REFUND_ACTIVITY_FOUND` |
| Shipment | order_id + items | Xác minh giao trễ bằng timestamp (không tin event đơn lẻ), xác định seller hay logistics | `handoff` `LATE_SELLER_HANDOFF` / `LATE_IN_TRANSIT` / `DELIVERY_ON_TIME` / `LATE_EVENT_CONTRADICTED` |
| Policy | `policy_version`, primary issue | Tải policy, ánh xạ issue → status, action, refund, responsible party | `policy_decided`, `handoff` → verifier |
| Verifier | Output nháp + evidence đã lấy | Kiểm tra invariant (mục 6) | `verification_completed` |

Quyền gọi tool (enforce bởi `TOOL_OWNERSHIP`, gọi sai quyền → `PermissionError`):

| Agent | Tools |
| --- | --- |
| order-agent | `get_order`, `get_order_items`, `get_sellers` |
| payment-agent | `get_payment_timeline`, `get_refund_timeline` |
| shipment-agent | `get_shipment_summary` |
| policy-agent | `get_policy` |

Không agent nào gọi `get_customer_history`, `get_product_context` hoặc `get_order_payments` (dữ liệu payment đã có trong timeline) vì không cần cho kết luận.

## 3. A2A protocol

- Envelope: `Finding(agent, decision_code, facts, evidence, conflicts)`. Coordinator gửi task qua `task_assigned` (target = agent, decision_code = mã task), specialist trả lời bằng `handoff` (target = coordinator, decision_code = kết luận domain, evidence_refs = ref đã dùng).
- Correlation: mọi event mang `case_id`. `CaseContext` được tạo mới cho mỗi case nên không chia sẻ state/evidence giữa các case.
- Luồng tuyến tính, mỗi task được giao đúng một lần, không có vòng lặp. Timeout mỗi case là 120s (`CASE_TIMEOUT_S`).
- Trace chỉ ghi event, decision code và evidence ref. Không ghi suy luận.

## 4. Evidence lifecycle

1. `EvidenceGateway.call` validate response theo `mcp-evidence-response-v1` schema.
2. `Agent.fetch` bọc thành `Evidence(tool, ref, domain, data)` và emit `tool_result_consumed` với đúng `evidence_ref` nhận được, không sửa hay tạo mới.
3. Lọc dữ liệu theo cửa sổ neo vào order row chuẩn:
   - items: `shipping_limit_date` ∈ [purchase, estimated_delivery]
   - payment events: ∈ [approved_at, approved_at + 1 ngày]
   - refund/shipment events: ∈ [purchase, max(opened_at, delivered_customer)]
   - bỏ row trùng hoàn toàn

   Row bị loại được ghi vào `data_conflicts` (`EXCLUDED_OUTSIDE_CASE_WINDOW` / `DEDUPLICATED_RECORD`).
4. Output chỉ trích dẫn evidence hỗ trợ kết luận: bộ tool theo từng primary issue (`classify()`) cộng với policy. Claim assessments dùng lại đúng tập ref này.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP timeout / mất kết nối | Có, tối đa 4 lần mỗi case, backoff 2s·n, session mới | Dừng run nếu vẫn lỗi | các event của lần thử lỗi vẫn còn, case được chạy lại từ đầu |
| Not found (tool error, ví dụ refund timeline rỗng) | Không (tất định) | Coi như không có bản ghi | `tool_result_consumed` / `TOOL_NO_RECORD` |
| Order không tồn tại / sai scope | Không | `insufficient_evidence`, `needs_investigation`, refund 0 | `handoff` / `ORDER_NOT_FOUND`, `ORDER_SCOPE_MISMATCH` |
| Source conflict | Không | Chọn row trong cửa sổ, loại row ngoài/trùng | `data_conflicts` trong output |
| Refund policy ≠ ước tính từ evidence | Không | Giữ số của policy, confidence 0.75 | `verification_completed` / `POLICY_EVIDENCE_AMOUNT_DIFFERS` |
| Invalid specialist result / invariant fail | Không | Hạ confidence xuống 0.6 | `verification_completed` / mã lỗi đầu tiên |

## 6. Verification invariants

- Tất cả `evidence_refs` trong output thuộc evidence mà case này đã lấy (ownership, không cross-case).
- `claim_assessments[].evidence_refs` ⊆ `evidence_refs` (claim linkage).
- `recommended_refund_brl` = tổng `refund_lines` (money totals).
- Refund > 0 ⇒ `action_required`; `action_required` ⇒ có `resolution_actions`.
- Party `seller` phải nằm trong `affected_entities.seller_ids` (policy trả seller mẫu, được thay bằng seller thực của order).
- So sánh refund của policy với ước tính độc lập từ evidence (captured total, freight, duplicate, mismatch, failed refund).
- Schema output được validate lại ở `cli.py` trước khi ghi file.

## 7. Reproducibility

- Không dùng LLM và không có randomness trong quyết định; cùng dữ liệu MCP cho ra cùng output (chỉ `evidence_ref`/`event_id` thay đổi mỗi lần chạy).
- Dependencies theo `pyproject.toml` (Python ≥ 3.11, `mcp>=2,<3`, `httpx2>=2,<3`).
- Chạy tuần tự, mỗi case một MCP session, timeout 120s/case, read timeout HTTP 60s.
- Lệnh: `day09 run && day09 validate && day09 package --output dist/submission.zip`.
- Cấu hình qua `.env` (`COMPETITION_API_URL`, `COMPETITION_TEAM_API_KEY`, `MCP_ENDPOINT`), không ghi key vào tài liệu hay trace.
