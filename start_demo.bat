@echo off
cd /d "%~dp0"
"C:\Users\Lokesh anand\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" run_agent.py --config config.yaml --demo
pause
