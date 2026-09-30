/** Vietnamese wording for English messages still returned by the GPU/installer backend. */
const exact: Record<string, string> = {
  'Request failed': 'Yêu cầu không thành công.',
  'Could not reach this host to inspect its SSH key.': 'Không kết nối được máy để đọc khóa SSH.',
  'Health and an actual workflow smoke test must pass before ready.': 'Cần vượt qua kiểm tra sức khỏe và chạy thử quy trình thực tế trước khi máy sẵn sàng.',
  'Inspect and confirm the SSH fingerprint before connecting.': 'Đọc và xác nhận fingerprint SSH trước khi kết nối.',
  'The server did not present an SSH host key.': 'Máy chủ không gửi khóa SSH để đối chiếu.',
  'Fingerprint does not match the currently inspected host key.': 'Fingerprint không khớp với khóa SSH vừa đọc. Đọc lại khóa rồi đối chiếu với nhà cung cấp.',
  'Confirm the SSH host key before running a recipe.': 'Xác nhận khóa SSH của máy trước khi chạy quy trình.',
  'Confirm the SSH host key before running preflight.': 'Xác nhận khóa SSH của máy trước khi kiểm tra máy.',
  'SSH identity changed. Start a new run.': 'Danh tính SSH của máy đã đổi. Hãy chạy lượt mới.',
  'A run is active on this host. Cancel it and wait before changing configuration.': 'Máy đang có quy trình chạy. Dừng và chờ nó kết thúc trước khi đổi cấu hình.',
  'Host configuration changed. Start a new run with the updated configuration.': 'Cấu hình máy đã đổi. Chạy lượt mới với cấu hình hiện tại.',
  'This legacy run has no safe snapshot. Start a new run.': 'Lượt cũ này không có bản chụp an toàn. Hãy chạy lượt mới.',
  'Only stopped runs can skip an optional step.': 'Chỉ bỏ qua được bước tùy chọn khi quy trình đã dừng.',
  'This step is required; it cannot be skipped.': 'Bước này bắt buộc, không thể bỏ qua.',
  'Step verification failed.': 'Kiểm tra kết quả của bước không đạt.',
  'Run a passing or warning-only preflight first.': 'Chạy kiểm tra máy đạt (hoặc chỉ có cảnh báo) trước.',
  'Run preflight to detect the GPU before choosing a profile.': 'Chạy kiểm tra máy để nhận diện GPU trước khi chọn cấu hình.',
  'GPU VRAM or system RAM is below the selected profile requirements.': 'VRAM GPU hoặc RAM của máy thấp hơn yêu cầu của cấu hình đã chọn.',
  'Unknown GPU profile.': 'Không có cấu hình GPU này.',
  'Stop the active tunnel before deleting this GPU connection.': 'Đóng tunnel đang mở trước khi xóa kết nối GPU này.',
  'Tunnel port must match the verified host configuration.': 'Cổng tunnel phải trùng với cấu hình máy đã kiểm chứng.',
  'Could not establish the SSH forward.': 'Không mở được kết nối chuyển tiếp qua SSH.',
  'A live tunnel and successful verification are required.': 'Cần tunnel đang mở và máy đã kiểm chứng thành công.',
  'Run Verify backend successfully before exposing a local URL.': 'Kiểm chứng backend thành công trước khi mở địa chỉ local.',
  'Backend version changed. Re-verify before exporting.': 'Phiên bản backend đã đổi. Kiểm chứng lại trước khi xuất.',
  'Backend version changed.': 'Phiên bản backend đã đổi.',
  'ComfyUI version changed. Run Verify backend again.': 'Phiên bản ComfyUI đã đổi. Chạy kiểm chứng backend lại.',
  'Endpoint is not a valid ComfyUI /system_stats response.': 'Địa chỉ này không trả về /system_stats hợp lệ của ComfyUI.',
  'ComfyUI exited on startup. Inspect the managed comfyui.log.': 'ComfyUI tắt ngay khi khởi động. Xem comfyui.log của dịch vụ.',
  'ComfyUI did not accept the complete smoke workflow.': 'ComfyUI không nhận đủ quy trình chạy thử.',
  'ComfyUI rejected the workflow. Check installed nodes, models and API-format inputs.': 'ComfyUI từ chối quy trình. Kiểm tra node, model đã cài và đầu vào dạng API.',
  'Smoke workflow completed without output. Use an output/save node.': 'Quy trình chạy thử xong nhưng không có output. Cần node lưu/xuất kết quả.',
  'Smoke workflow failed on the GPU. Check ComfyUI logs and model compatibility.': 'Quy trình chạy thử lỗi trên GPU. Xem log ComfyUI và độ tương thích model.',
  'Smoke workflow timed out; the host is not ready.': 'Quy trình chạy thử quá thời gian; máy chưa sẵn sàng.',
  'Save an API-format smoke workflow in host configuration before verification.': 'Lưu quy trình chạy thử dạng API trong cấu hình máy trước khi kiểm chứng.',
  'Save a real API-format workflow under Host configuration before the smoke test.': 'Lưu quy trình dạng API thật trong cấu hình máy trước khi chạy thử.',
  'Paste the API-format node graph, not the UI workflow or a prompt wrapper.': 'Dán graph node dạng API, không phải workflow giao diện hay phần bọc prompt.',
  'Workflow exceeds 1 MB.': 'Quy trình vượt quá 1 MB.',
  'Adopt mode is read-only except the approved smoke workflow. Use Verify backend.': 'Chế độ dùng lại ComfyUI có sẵn chỉ đọc, trừ quy trình chạy thử đã duyệt. Dùng Kiểm chứng backend.',
  'Adopt mode never starts/stops an externally managed process.': 'Chế độ dùng lại không bật/tắt tiến trình do nơi khác quản lý.',
  'Choose a dedicated install directory, for example /workspace/ghm.': 'Chọn thư mục cài riêng, ví dụ /workspace/ghm.',
  'The managed ComfyUI install or virtualenv is missing.': 'Thiếu bản cài ComfyUI hoặc môi trường Python do Studio quản lý.',
  'No live owned ComfyUI process; refusing runtime maintenance.': 'Không thấy tiến trình ComfyUI do Studio quản lý; không bảo trì runtime.',
  'Process ownership mismatch; refusing to stop or replace it.': 'Tiến trình không thuộc Studio; không dừng hay thay thế.',
  'Managed process did not stop in 10 seconds.': 'Tiến trình không dừng trong 10 giây.',
  'Gated models need a Hugging Face token in Settings.': 'Model giới hạn quyền cần token Hugging Face trong Cài đặt.',
  'Enter a valid read-only Hugging Face token, or an empty value to remove it.': 'Nhập token Hugging Face chỉ-đọc hợp lệ, hoặc để trống để xóa.',
  'Hugging Face metadata lookup failed. Check repo, exact filename and access token.': 'Không đọc được thông tin trên Hugging Face. Kiểm tra repo, đúng tên file và token.',
  'Insufficient free space on the model volume for the missing files; nothing was downloaded.': 'Ổ chứa model không đủ chỗ cho các file còn thiếu; chưa tải gì.',
  'Set GHM_MASTER_KEY or install a keyring backend.': 'Đặt GHM_MASTER_KEY hoặc cài keyring để mã hóa thông tin đăng nhập.',
  'Invalid event cursor.': 'Vị trí nhật ký không hợp lệ. Tải lại nhật ký.',
};

const patterns: [RegExp, (m: RegExpMatchArray) => string][] = [
  [/^Remote command exited (?:with status )?(-?\d+)\.$/, m => `Lệnh trên máy GPU kết thúc với mã lỗi ${m[1]}.`],
  [/^Profile requires NVIDIA driver (\S+)\+; update it outside this app\.$/, m => `Cấu hình này cần driver NVIDIA ${m[1]} trở lên; cập nhật driver bên ngoài ứng dụng.`],
  [/^Workflow node (\S+), input (\S+): (.+)$/, m => `Node ${m[1]}, đầu vào ${m[2]}: ${m[3]}`],
  [/^([\w.@/+, -]+): download\/verification failed\. Check free disk, HF access and checksum\.$/, m => `${m[1]}: tải hoặc kiểm tra checksum thất bại. Kiểm tra chỗ trống, quyền Hugging Face và checksum.`],
  [/^(.+): add a read-only Hugging Face token in Settings\.$/, m => `${m[1]}: thêm token Hugging Face chỉ-đọc trong Cài đặt.`],
];

const vietnamese = /[À-ỹ]/;

/** Vietnamese text for a backend message, or null when it is unknown English. */
export function viMessage(message: string): string | null {
  const text = message.trim();
  if (exact[text]) return exact[text];
  for (const [pattern, render] of patterns) {
    const match = text.match(pattern);
    if (match) return render(match);
  }
  return vietnamese.test(text) ? text : null;
}

/** Always-displayable text: Vietnamese when known, otherwise the original message. */
export const showMessage = (message: string) => viMessage(message) ?? message;
