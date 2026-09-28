# Historia — Video, Nhật ký và Tác vụ

Trạng thái: đã triển khai local theo plan hai tab riêng, giao diện tối.

## Giao diện

- Điều hướng dự án: Ý tưởng, Kịch bản, Video, Nhật ký. Video và Nhật ký có nền than tối; các trang soạn nội dung/GPU giữ giao diện hiện có.
- Video: chọn lượt sản xuất, điều khiển pause/resume, số shot đã lưu, thời lượng audio, ETA/chi phí theo số đo. Một player, danh sách cảnh bên phải, dải shot của cảnh chọn. Phân biệt cảnh đang xem và cảnh đang sản xuất.
- Cảnh/shot/lượt được lưu trong URL; polling không đổi lựa chọn. Quay lại từ log khôi phục vị trí phát trong phiên và không autoplay. File lỗi có thông báo và nút tải lại media, không render lại.
- Thông số mở trong inspector; chỉnh steps phần chưa gửi chỉ khi pause. Benchmark nằm trong nâng cao.
- Nhật ký: một reader, tìm kiếm toàn lịch sử phía server, lọc run/cảnh/job/công đoạn/mức độ, phân trang, virtual list, theo dõi dòng mới, xem đầy đủ sự kiện và tải NDJSON.
- Log cũ không được đoán mức độ/shot từ chuỗi. Khi lọc shot, sự kiện chung hoặc legacy vẫn hiện kèm nhãn Toàn tác vụ.
- Tác vụ: bảng gọn có tên dự án/cảnh, trạng thái, tiến độ, thời gian và GPU; kết quả và thao tác ở vùng chi tiết. Log bộ cài cũng mở trong viewer chung từ trung tâm Tác vụ.
- Chỉ dùng log Studio đã lưu, chưa stream terminal/Comfy thô. Hành động nguy hiểm giữ xác nhận và điều kiện backend.

## API và dữ liệu

API cũ `/api/studio/jobs/{id}/events` vẫn trả list như trước.

- `GET /api/studio/projects/{id}/events`: journal dự án.
- `GET /api/studio/jobs/{id}/journal`: journal một job, kể cả job không thuộc dự án.
- Thêm `/export` vào hai URL trên để tải NDJSON toàn bộ bộ lọc, chốt ID cuối lúc bắt đầu tải.
- Bộ lọc: `run_id`, `scene_id`, `job_id`, `kind`, `stage`, `level` (có thể phân cách bằng dấu phẩy), `q`.
- Cursor: `after` hoặc `before`, `tail=true` mặc định, `limit=200` (tối đa 500). Response: `items`, `has_more`. Event có job/cảnh/công đoạn, timestamp, message và metadata nếu có.
- Performance thêm `run_id` tùy chọn, trả danh sách shot có index, stage, job ID, artifact ID, config, timing và trạng thái.
- `studio_job_events.context` là JSON nullable; DB SQLite cũ được online backup trong `backups/` trước ALTER TABLE. Không rewrite event, snapshot hoặc artifact cũ.
- Sự kiện gửi prompt/tải output/lưu artifact được ghi ở checkpoint; lỗi đã phân loại được ghi khi job đổi error. Không dump env/credential/argv vào log.

## Kiểm thử và chạy thử

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_studio_journal.py tests/test_wan_performance.py -q
cd ghm/frontend
npm run build
npm run lint
npx playwright install chromium
npm run test:e2e
```

Playwright tự chạy Vite local tại port 4175 và mock API; không kết nối hoặc thuê GPU. Fixture gồm 14 cảnh/70 shot, hơn 1.200 log và video màu đơn sắc 3 giây. Đây là dữ liệu kiểm thử, không phải kết quả AI. Ảnh chụp responsive nằm trong `ghm/frontend/test-results/` (được gitignore).

Backend kiểm tra backup/migration, event cũ, cursor, tìm/lọc, export hơn 1.000 dòng, scope dự án/run và checkpoint. Browser kiểm tra một player, lựa chọn ổn định, vị trí phát sau đổi tab, log đúng ngữ cảnh, không ép cuộn, export, mất kết nối, pause đúng một POST, trạng thái sản xuất và bố cục 1920/1366/1024/768.

Không thay thuật toán AI, prompt ID hoặc điều kiện reconcile; không gọi GPU để nghiệm thu UI. ETA thiếu mẫu vẫn là chưa biết. Tiền thuê Pod không được suy ra từ trạng thái pause.
