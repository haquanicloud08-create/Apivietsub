# VietSub AI — backend xử lý video

## Các chức năng đã tích hợp
1. Tách âm thanh và nhận dạng lời thoại bằng OpenAI Whisper (`whisper-1`).
2. Dịch các đoạn lời thoại bằng `gpt-4o-mini`.
3. Tạo giọng đọc bằng OpenAI TTS (`gpt-4o-mini-tts`).
4. Ghép giọng đọc/phụ đề và xuất MP4 bằng FFmpeg; có file SRT tải riêng.

## Deploy lên Render
- Đặt `app.py`, `requirements.txt`, `render.yaml` ở thư mục gốc repository.
- Đặt giao diện tại `templates/index.html`.
- Trong Render > Environment, cấu hình `OPENAI_API_KEY` bằng API key của bạn. Không ghi key vào HTML, GitHub hoặc gửi cho người khác.
- Commit/push các file rồi chờ Render deploy thành công. Mở `/health`; cần thấy `openai_api_key_configured: true` và `translation_engine: openai-whisper-gpt-4o-mini-tts`.
- Thử với video ngắn trước.

## API
- `GET /health`: kiểm tra backend, API key và FFmpeg.
- `POST /api/upload`: tải video lên.
- `POST /api/jobs/<id>/start`: bắt đầu xử lý nền.
- `GET /api/jobs/<id>`: theo dõi tiến độ.
- `GET /api/album`: liệt kê đầu ra.

## Lưu ý quan trọng
- API key phải có quyền truy cập API và tài khoản có hạn mức/thanh toán phù hợp. API có thể tính phí theo thời lượng và số đoạn giọng đọc.
- Video dài có thể mất nhiều thời gian; giới hạn dung lượng thực tế còn phụ thuộc gói Render.
- Dịch/lồng tiếng từng đoạn có thể không khớp khẩu hình hoàn toàn.
- Render filesystem mặc định có thể không bền vững sau khi service khởi động lại; trạng thái tác vụ hiện lưu trong bộ nhớ và có thể mất nếu server restart.
- `render.yaml` cài FFmpeg bằng `apt-get`; nếu build log báo không cài được FFmpeg, cần dùng môi trường hỗ trợ gói hệ thống hoặc chỉnh cách cài FFmpeg.
