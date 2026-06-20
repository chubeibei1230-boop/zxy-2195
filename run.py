from dotenv import load_dotenv
load_dotenv()

from app import create_app
from config import Config

app = create_app()

if __name__ == '__main__':
    print(f"========================================")
    print(f"  语言测评管理系统 API")
    print(f"  服务地址: http://127.0.0.1:{Config.PORT}")
    print(f"  数据库路径: {Config.DATABASE_PATH}")
    print(f"========================================")
    print(f"  默认账号：")
    print(f"  管理员: admin / admin123")
    print(f"  阅卷员: reviewer1 / reviewer123")
    print(f"  阅卷员: reviewer2 / reviewer123")
    print(f"  复核员: auditor1 / auditor123")
    print(f"========================================")
    app.run(host='0.0.0.0', port=Config.PORT, debug=False)
