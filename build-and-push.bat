@echo off
REM 构建并推送 Docker 镜像 (Windows)

setlocal

set IMAGE_NAME=code-executor-service
set VERSION=%1
if "%VERSION%"=="" set VERSION=latest

echo === Building Code Executor Service ===
echo Version: %VERSION%

REM 构建镜像
echo Building Docker image...
docker build -t %IMAGE_NAME%:%VERSION% .

if errorlevel 1 (
    echo Build failed!
    exit /b 1
)

echo === Done ===
echo Image built: %IMAGE_NAME%:%VERSION%
echo.
echo To push to Docker Hub:
echo   docker login
echo   docker tag %IMAGE_NAME%:%VERSION% your-username/%IMAGE_NAME%:%VERSION%
echo   docker push your-username/%IMAGE_NAME%:%VERSION%

endlocal
