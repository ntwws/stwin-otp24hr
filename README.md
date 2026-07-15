# OTP24HR by STWIN

โปรแกรม Windows สำหรับจัดการหมายเลข LINE ประเทศไทยและรับ OTP ผ่าน API

## ฟังก์ชันหลัก

- Login และกำหนดสิทธิ์ `user` / `admin` ผ่าน Cloudflare
- ซื้อและติดตามหลายหมายเลขพร้อมกัน
- ตรวจ OTP อัตโนมัติ พร้อมเสียงแจ้งเตือน
- ขอ OTP ซ้ำ เสร็จสิ้น และยกเลิกหมายเลข
- รายงานยอดใช้งานรายเดือนและประวัติเบอร์
- อัปเดตโปรแกรมอัตโนมัติผ่าน GitHub Releases

## ดาวน์โหลด

ดาวน์โหลดไฟล์ Portable จากหน้า [Releases](https://github.com/ntwws/stwin-otp24hr/releases)
แล้วแตก ZIP ก่อนเปิด `OTP24HR by STWIN.exe`

## สร้างโปรแกรมจาก Source

ต้องใช้ Python 3.12 หรือใหม่กว่า:

```powershell
python -m pip install -r requirements.txt
.\build-exe.bat
```

ไฟล์ที่สร้างจะอยู่ใน `dist\OTP24HR by STWIN\`

> ใช้งานหมายเลขและบัญชีตามข้อกำหนดของผู้ให้บริการและกฎหมายที่เกี่ยวข้อง
