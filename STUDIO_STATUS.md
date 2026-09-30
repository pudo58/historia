# Historical Video Studio — trạng thái triển khai

## Chạy local

```powershell
.\.venv\Scripts\python.exe -m ghm.cli serve --port 8000
```

Mở http://127.0.0.1:8000. API và UI dùng chung địa chỉ; 8765 là giao diện VV cũ, không phải Studio mới.
Build UI: chạy `npm ci` rồi `npm run build` trong `ghm/frontend`.
Dependency local khai báo trong `pyproject.toml`; không cần cài model AI lên Windows.

## Đã nối vào bản chạy

- Giao diện dự án tiếng Việt; quản lý GPU cũ giữ ở mục riêng, API host/export-backend được bảo toàn.
- Dự án, nguồn, hồ sơ nhân vật/bối cảnh/trang phục, dàn ý thủ công, cảnh và trạng thái duyệt lưu SQLite.
- Sao lưu SQLite bằng online backup vào `backups/` trước lần thêm bảng Studio đầu tiên; không xóa host cũ.
- Nhập TXT/Markdown/PDF có chữ/ảnh/video/nhạc với giới hạn dung lượng và kiểm tra tệp.
- PDF scan báo cần OCR; PDF thiếu chữ ở một số trang được cảnh báo. Video tham khảo trích 8 khung hình tại local.
- Tách nguồn lịch sử khỏi ảnh tham khảo; kiểm tra đoạn trích nguyên văn và quyền sở hữu nguồn theo dự án.
- Duyệt lời đọc/ảnh/clip theo revision; sửa lời đọc giữ ảnh và làm mất hiệu lực âm thanh/duyệt clip.
- Artifact có SHA-256, giới hạn đường dẫn và kiểm tra trước khi đọc/xuất.
- Hàng đợi lưu DB, log và snapshot; export local MP4/SRT/kịch bản có trích dẫn/cấu hình đã kiểm thử với media tổng hợp.
- Transport ComfyUI lưu prompt ID trước khi POST; trường hợp chưa rõ chuyển sang đối chiếu, không tự gửi lại.

## Chưa nghiệm thu — không coi là đã hoàn thành plan

- Cài tự động đã mở qua mục **Bộ AI & kiểm chứng**: tự dò môi trường → preview metadata/checksum/dung lượng → xác nhận → job cài → job kiểm chứng output. Runpod Basic SSH đã có cầu nối PTY cho lệnh, file và HTTP loopback; không còn bắt đổi sang Full SSH. Chưa nghiệm thu toàn bộ cài/model/AI trên máy thuê sạch.
- Chưa có GPU thật chạy thành công Qwen Image/Edit, Wan2.2, Qwen3-VL, VieNeu; chưa có bản phim mẫu AI 30–60 giây. Tác vụ AI khóa khi installation chưa verified, không có đường tắt trong UI để gắn nhãn verified.
- Cần rà soát dependency transitive và lockfile môi trường; đo dung lượng cài bổ sung thay vì tổng bộ model; validate các tham số/node theo ComfyUI đã ghim.
- Wizard dò các đường dẫn ComfyUI phổ biến và port 8188/8190; tự đề xuất tái sử dụng khi chỉ có một môi trường/dịch vụ. ComfyUI path độc lập với thư mục dịch vụ Studio; cấu hình thủ công thu gọn. Chưa tự phát hiện tất cả môi trường trên máy, xác minh volume bền vững theo nhà cung cấp hoặc dự toán chi phí phim dài.
- Cần kiểm thử GPU thật cho OOM/hết ổ/mất SSH/cancel/recovery, không chỉ unit test local.
- Kịch bản AI dài cần chia theo chương thay vì một lượt sinh; chưa có kiểm thử 10–20 phút và dự toán từ benchmark.
- Chưa mở ghép nhạc, phụ đề theo cụm ngắn, batch render, chỉnh sửa/xóa/reorder toàn bộ loại tài nguyên, nhiều trích dẫn trong form, hay phân tích ảnh tham khảo tự động.
- Bản nháp hiện cấu hình 832×480; bản chính 1280×720. Không tự giảm chất lượng do thiếu VRAM.
- Giao diện GPU legacy đã Việt hóa nhãn và các lỗi backend thường gặp; lỗi tiếng Anh hiếm gặp vẫn hiện nguyên văn trong phần thông tin kỹ thuật. Chưa gộp thành wizard một luồng.

## Kiểm thử

### Duyệt ảnh theo lô, tiến độ theo cảnh, tách main.tsx (29/09/2026)

- Backend: cảnh ảnh riêng từng shot/shot list sinh xong keyframe không còn dừng cả lượt. Cảnh được đưa vào hàng chờ `review_pending`; GPU tiếp tục sinh keyframe các cảnh còn lại. Chỉ khi hết keyframe mà còn cảnh chưa duyệt thì lượt chuyển `keyframe_review` và Wan chưa chạy. Checkpoint cũ chỉ có `review_scene_id` vẫn đọc được.
- `POST /api/studio/production-runs/{id}/approve-keyframes` nhận `{"scene_ids":[...]}` (vẫn nhận `scene_id` cũ), duyệt được sớm khi lượt đang chạy/tạm dừng; duyệt nhiều cảnh là tất cả hoặc không, vẫn kiểm tra đủ ảnh và checksum. Kiểm ảnh Qwen3-VL vẫn chỉ mở khi lượt ở `keyframe_review`.
- Video: khung duyệt ảnh cho mọi cảnh đang chờ (ảnh lớn, dải shot, lời dẫn, phím J/K đổi cảnh, ←/→ đổi ảnh, A duyệt). "Duyệt N cảnh đã xem" chỉ gồm cảnh đã mở trong phiên. Phím tắt bỏ qua khi đang gõ trong ô nhập hoặc có hộp thoại.
- Dòng "việc tiếp theo" thay dòng trạng thái cũ; bảng "Tiến độ theo cảnh" (Giọng đọc → Ảnh → Duyệt ảnh → Clip → RIFE, ghép phim) nằm dưới trình phát.
- `main.tsx` 64 KB tách thành `App`, `ProjectView`, `LegacyProjectView`, `ProjectForm`, `ProjectLibrary`, `Sources`, `SceneEditor`, `api.ts`, `ui.tsx`; không đổi hành vi.
- Việt hóa: `messages.ts` dịch các lỗi tiếng Anh thường gặp từ backend GPU/bộ cài (fingerprint, preflight, tunnel, smoke test, tải model); phương thức xác thực và nhãn topbar đã Việt hóa. Lỗi chưa có trong bảng vẫn hiện nguyên văn trong "Thông tin kỹ thuật gốc".
- Kiểm thử: backend 298 test pass (thêm test không để GPU rảnh khi chờ duyệt và duyệt lô tất cả-hoặc-không). Frontend build/TypeScript/ESLint pass. Playwright 28 test: 26 pass trên Chromium không có H.264; 2 test phát video MP4 cần trình duyệt có H.264 và pass khi thay video mẫu bằng WebM trong lần chạy kiểm tra. Chưa thử với lượt sản xuất thật trên GPU.

### Tăng tốc bộ cài trên Pod mới (29/09/2026)

- Số đo trước khi sửa (job `b70e87e1`, 28/09): tổng 20,7 phút; tải 11 model tuần tự chiếm 13,5 phút (~125 MB/s, một kết nối), ba venv PyTorch ~4–5 phút chạy sau khi tải xong.
- Tải model theo lô trong một lệnh remote: tối đa 4 file cùng lúc (file lớn trước), SHA256/git-sha1 tính ngay khi tải nên không đọc lại file sau khi xong; file `.part` vẫn resume bằng Range, file sai checksum vẫn bị cách ly, file có sẵn sai checksum vẫn không bị ghi đè. Kiểm tra chỗ trống cho toàn bộ phần còn thiếu trước khi tải.
- File model LLM/TTS (Qwen3-VL, VieNeu, codec) tải chung lô này qua URL revision cố định; bước kiểm tra snapshot dịch vụ cũ vẫn chạy nhưng chỉ còn xác minh.
- ComfyUI được checkout trước, sau đó tải model song song với cài PyTorch/requirements và venv LLM/TTS. Tải model dùng khóa flock riêng (`-models.lock`) nên không chặn pip; downloader cũ còn chạy vẫn chặn downloader mới. Lỗi ở một nhánh hủy nhánh còn lại, file `.part` giữ lại để resume.
- Chỉ chạy song song khi thăm dò thấy hai lệnh SSH thực sự chạy cùng lúc (hai lệnh `sleep 2` xong trong dưới 3,5 giây). Gateway Basic SSH từ chối hoặc xếp hàng kênh thì tự quay về thứ tự tải xong rồi cài. **Chưa thử trên Runpod Basic SSH thật** nên chưa biết gateway có cho hai kênh đồng thời không.
- Sửa lỗi: tiến độ tải trước đây không vào log vì `InstallExecutor.run_input` bỏ `on_output`. Nay log hiện "Đã tải X / Y GiB model" và từng file đã kiểm tra checksum.
- Kiểm thử local: chạy script tải thật với server HTTP hỗ trợ Range (song song, resume, server bỏ qua Range, sai checksum, file có sẵn sai checksum, đích ngoài thư mục cho phép, thiếu chỗ trống); orchestration install song song/tuần tự/hủy khi lỗi. Toàn bộ 296 test pass, Ruff pass. Thử nghiệm giới hạn 25 MB/s mỗi kết nối: 4 luồng nhanh gấp ~2,7 lần 1 luồng. Chưa đo trên Pod thật; mức giảm thực phụ thuộc mạng của máy chủ.

### Chất lượng và chi phí render (28/09/2026)

- Cảnh mới chọn Wan (mặc định 81 frame, 16 fps, Lightning 4 bước, một ảnh chung), Ken Burns hoặc ảnh tĩnh. Hai chế độ local tạo clip 24 fps phủ đúng audio; Wan có thể dùng ảnh riêng từng shot (duyệt cả bộ) hoặc frame cuối shot trước (duyệt từng frame trước khi gửi shot tiếp).
- Cảnh v3 có thể rút riêng shot Wan cuối còn `4n+1` frame trong khoảng 33–81; tùy chọn này và `clip_steps` cũng áp dụng được cho shot chưa gửi khi pause. Intent đã có `prompt_id`, shot hoàn thành, seed, chỉ số shot và audio không đổi. Snapshot cũ vẫn dùng hành vi cũ.
- Qwen-Image text có hồ sơ Lightning 4 bước tùy chọn với LoRA fp8 riêng và checksum/revision; hồ sơ 20 bước vẫn mặc định. RIFE 4.26 là job hậu kỳ riêng: Comfy 16→48 fps, local lấy mẫu 24 fps và lưu artifact riêng. Có thể xuất lại từ clip Wan cũ. Cả hai model là cài đặt tùy chọn, phải xác minh checksum trên host trước khi dùng.
- Kịch bản AI mới dùng prompt hình tiếng Anh, lời đọc/trích dẫn tiếng Việt. Có thể cấu hình API OpenAI-compatible HTTPS; khóa nằm trong LocalSetting mã hóa. Không có cấu hình API thì dùng Qwen3-VL local; lỗi API không tự chuyển model, trạng thái gửi chưa rõ chuyển sang đối chiếu. Kiểm ảnh Qwen3-VL chỉ gắn cờ/so sánh, không tự duyệt.
- Export có thể ghép **một** tệp âm thanh được đánh dấu “Sử dụng” trong thư viện. Hồ sơ `voice_duck_v1` tùy chọn dùng sidechaincompress, amix normalize=0, loudnorm mục tiêu −16 LUFS/−1,5 dBTP và lưu số đo LUFS/peak. Chưa bật hồ sơ này làm mặc định cho final vì cần nghe thử. Cảnh thường được nén H.264 một lần; khi cần hòa hình dùng trung gian FFV1.
- Video gợi ý mở RunPod sau 10 phút nhàn rỗi chỉ khi đọc được queue Comfy và không có prompt/job cần đối chiếu; không gọi API dừng Pod. Nếu không kiểm tra được queue, hiển thị chưa xác minh.
- Kiểm thử local gồm graph/API checkpoint, mode ảnh tĩnh, RIFE timing và mix media tổng hợp. **Chưa thuê A100 cho A/B**, chưa có median hot shot, GPU-hours/chi phí thực hay đánh giá lỗi chuyển động. Cần chốt giới hạn tiền thuê trước khi benchmark ít nhất 5 shot nóng mỗi cấu hình.

### Video / Nhật ký / Tác vụ mới (28/09/2026)

- Video và Nhật ký là hai tab riêng, nền tối kiểu phòng dựng. Video có một player, danh sách cảnh/shot, bộ chọn lượt và inspector; Tác vụ dùng bảng gọn có tên dự án/cảnh.
- Nhật ký có lọc/tìm toàn lịch sử phía server, cursor, virtual list, tải NDJSON, trạng thái kết nối riêng; log bộ cài mở cùng viewer. Không stream terminal hoặc Comfy thô.
- Migration bổ sung JSON context cho event cũ có online backup SQLite trước khi đổi schema. Giữ dữ liệu cũ và API events theo job.
- Regression backend: 233 test pass (hai test SSH cách ly default keypair trong tiến trình test như ghi chú trên). Sau bổ sung sự kiện lưu artifact, chạy lại bộ journal/Wan: 39 pass. Playwright: 12 pass, gồm responsive 1920/1366/1024/768, video mẫu thật, pause một POST, log lớn và các trạng thái sản xuất. Build/TypeScript, ESLint, Ruff và diff-check pass.
- Chỉ dùng fixture local, không gọi/thuê GPU, không restart backend người dùng. Chi tiết vận hành/API: [STUDIO_UI_REDESIGN_PLAN.md](STUDIO_UI_REDESIGN_PLAN.md).

### Tối ưu Wan và đo thời gian (28/09/2026)

- Tách steps ảnh/clip, giữ cách đọc snapshot cũ. Wan mặc định vẫn 81 frame / 16 fps / 4 bước, không giảm độ phân giải.
- Cache upload/schema trong kết nối clip job; kiểm tra thay đổi process/runtime; giữ prompt ID trước POST và tải lại output đã xong khi history mất.
- Đổi steps phần chưa gửi qua checkpoint khi pause; giữ artifact, seed và shot đã gửi. UI có thông số thực, timing, ETA/chi phí xử lý theo cấu hình tương thích.
- Runtime SageAttention/highvram tùy chọn, không tự bật. Quản lý riêng tiến trình thuộc Historia; Comfy adopt chỉ đọc/hướng dẫn. Benchmark có phí là thao tác riêng, gồm một shot đầu và ít nhất năm shot nóng.
- Kiểm thử local: toàn bộ regression 227 test pass; sau kiểm tra cuối, bộ Wan/performance 35 test pass. Ruff các file Python thay đổi và frontend build/type-check/lint pass. Test SSH được cách ly khỏi default keypair trên máy trong tiến trình test; không sửa/xóa khóa người dùng.
- Hướng dẫn API, khôi phục, giới hạn và quy trình A100: [STUDIO_PERFORMANCE.md](STUDIO_PERFORMANCE.md). Chưa thuê/chạy A100, chưa xác nhận tốc độ 2× hoặc chất lượng GPU thật.

### Sửa cài LLM trên template Runpod có PIP_CONSTRAINT (27/09/2026)

- Tái hiện trên Pod thật: venv LLM chỉ có pip; lệnh torch==2.8.0 bị constraint kế thừa ép torch==2.11.0+cu130 và trả ResolutionImpossible. Không phải lỗi GPU/model.
- Lệnh pip của venv Studio loại bỏ PIP_CONSTRAINT/PIP_BUILD_CONSTRAINT và các override đích cài cho tiến trình con, dùng PIP_CONFIG_FILE=/dev/null. Không sửa biến môi trường/template toàn cục hoặc venv ComfyUI.
- Báo lỗi theo công đoạn LLM/TTS/PyTorch cùng nhóm nguyên nhân (dependency, ổ đĩa, wheel, mạng/chứng chỉ); không lưu log pip thô có thể chứa token.
- Tác vụ install failed có thể tiếp tục bằng snapshot đã chấp thuận, vẫn kiểm tra host/fingerprint/cấu hình/khóa. Không tự retry tác vụ AI thất bại. Đã gửi resume cho job 245ac437-c908-4c90-ab52-869f17bffc47 theo yêu cầu sửa lỗi của người dùng.
- 78 test pass; Ruff, frontend build/type-check/lint pass. Việc resume đang chạy không được coi là cài xong hoặc workflow đã verified.

### Runpod Basic SSH và đơn giản hóa bộ cài (27/09/2026, bản sửa mới)

- PTY RPC một lần, chạy trong bộ nhớ, không daemon/agent thường trú, không mở cổng công khai. Giữ kiểm tra fingerprint. Bootstrap chờ raw-mode READY trước khi gửi stdin; nhận mã thoát thực; EOF không được coi là thành công và không tự gửi lại lệnh.
- HTTP qua SSH chỉ tới port loopback đã chọn; không theo redirect. Truyền file có checksum SHA256, tải về file tạm rồi promote. Upload tối đa 32 MiB/lần, download file tối đa 2 GB; model lớn được tải trực tiếp trên GPU, không đi qua giới hạn upload này.
- Thử thật với `test1`: chạy lệnh/stdin Unicode, nhận rc=7 đúng; gọi `/system_stats` nhận ComfyUI 0.36.0 port 8188; truyền hai chiều 238811 byte khớp SHA256. File/thư mục test riêng trên Pod đã được xóa, không xóa dữ liệu người dùng.
- Bấm **Kiểm tra & chuẩn bị cài** trên UI thật thành công: `/ComfyUI` có sẵn, dịch vụ riêng `/workspace/historia`; 2 file model hợp lệ; còn 94.43 GiB model, dự tính cần trống 129.93 GiB gồm 35.50 GiB dự phòng; ổ trống 181.64 GiB tại lần kiểm tra. Đối chiếu `/object_info` có đủ 20 loại node trong ba graph.
- Preview kiểm tra chỗ trống riêng theo filesystem model/dịch vụ. Pod thiếu espeak-ng; sau consent bộ cài bổ sung công cụ hệ thống bằng apt, giữ nguyên Python environment và tiến trình ComfyUI đã có (chặn nếu queue đang bận).
- UI có nút mở bộ cài ngay tại Kết nối GPU; cấu hình nâng cao thu gọn, không dùng Run recipe để cài bộ Studio. Log download chỉ hiển thị dòng số byte đã lọc, không lưu output pip/HF thô; UI lấy 500 sự kiện mới nhất.
- Chưa bấm chấp thuận license hoặc tải/cài 94.43 GiB trên Pod. Preview/transport thành công không đồng nghĩa toàn bộ model đã cài hoặc workflow đã verified. Kiểm chứng ảnh/clip/giọng thật vẫn là bước riêng.
- Test protocol bao gồm EOF/rc/error, stdin không xuất hiện trong bootstrap, binary/checksum, từ chối HTTP khác đích, cancel đóng channel, giữ output cũ khi checksum sai; thêm test filesystem riêng, lọc secret khỏi progress và log tail.
- Kết quả cuối: 76 test pass; Ruff trên module thay đổi và frontend lint/build/type-check pass. Có một cảnh báo deprecation từ Starlette TestClient.

### Nền tảng bộ cài (27/09/2026)

- API `/api/studio/hosts/{id}/installation`: GET trạng thái; POST `/prepare`, `/start`, `/verify`.
- Preview chỉ đọc SSH và metadata Hugging Face, không cài/download; hết hạn sau một giờ. Snapshot chứa host/fingerprint, root/port, model revision/checksum, graph hashes và dependency pins. Consent bắt buộc; không nhận graph/repository tùy ý.
- Đã resolve metadata thật cho 11 model ComfyUI và 3 bộ dịch vụ: khoảng 100,94 GiB (có thể đổi ở lần resolve kế tiếp); URL Lightning đã sửa sang repo chính thức lightx2v/Qwen-Image-Lightning.
- SHA256 cho LFS và Git blob SHA1 cho file nhỏ trong snapshot dịch vụ. File hợp lệ được giữ, file sai checksum không bị ghi đè. Preview tính model thiếu + 35 GiB dự phòng, kiểm tra lại khi thực thi.
- Không sửa driver, không tự stop ComfyUI, không thay venv không thuộc Studio; chặn queue đang bận, root/symlink không an toàn và filesystem thiếu chỗ. Pip/HF output thô không lưu để tránh lộ signed URL/token.
- Job install/verify lưu DB; bấm lặp trả cùng job. Restart/ngắt không tự cài lại; resume kiểm tra snapshot và khóa flock remote, không hứa tiếp tục giữa bước diffusion. Mất SSH giữ trạng thái cần đối chiếu.
- `installed` khác `verified`. Verify chạy Qwen Image, Edit, Wan clip, tiếng Việt và Qwen3-VL; chỉ đánh dấu verified khi output local đọc được và JSON LLM hợp lệ. Artifact kiểm chứng tải được qua API/UI.
- Test tự động gồm API/FakeExecutor/FakeBackend với media tổng hợp, không phải benchmark GPU. E2E cài trọn bộ trên Pod test1 còn chờ xác nhận tải/cài của người dùng.
- Frontend lint/build/type-check pass; UI đã kiểm tra trực tiếp trên localhost:8000 với test1. Không tạo job cài trên dữ liệu thật.
- Dependency trực tiếp đã ghim, phiên bản package đã đối chiếu PyPI; dependency transitive chưa khóa toàn bộ. Lưu pip freeze môi trường dịch vụ sau cài. Không công bố bộ này là đã được chứng nhận trên GPU nào trước smoke test thật.

### Sửa preflight Runpod Basic SSH (27/09/2026)

- Đã kiểm thử đọc thông tin thật qua API với host `test1`: RTX PRO 4500 Blackwell 31,86 GiB VRAM; driver 580.126.16; CUDA toolkit 13.0; Python 3.12.3.
- Basic SSH dùng shell PTY, đóng khung kết quả có mã thoát từng lệnh; không nhầm phản hồi gateway mã 0 với lệnh thành công. Các recipe ghi dữ liệu không được tự retry qua PTY.
- Tính RAM theo giới hạn cgroup nếu có (không dùng tổng RAM node vật lý làm RAM Pod); kết quả lưu và hiện lại khi reload.
- WARN về SCP/SFTP/Full SSH tách biệt với GPU PASS. Đây chỉ là kiểm chứng preflight, không phải kiểm chứng workflow AI hoặc cài đặt.
- 43 test pass; frontend lint/build pass.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check studio tests/test_studio.py
```

Frontend: `npm run lint`, `npm run build` (bao gồm TypeScript strict).
Các test media tạo video màu đơn sắc và WAV im lặng để kiểm tra ghép/độ dài/phụ đề, **không phải kết quả AI và không chứng minh chất lượng model**.

Giữ cùng Windows keyring hoặc `GHM_MASTER_KEY` đã dùng để giải mã host cũ. Nếu thay khóa, cần nhập lại credential; không dùng demo master key và không xóa DB để xử lý lỗi.
Dừng app/tunnel/render không dừng tiền thuê GPU.
