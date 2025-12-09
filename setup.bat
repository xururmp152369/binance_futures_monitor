# setup.bat（Windows）
@echo off
python -m venv venv
venv\Scripts\activate
pip install --upgrade pip
pip install -r requirements.txt
echo 環境建置完成！直接執行 python main.py
pause