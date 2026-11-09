@echo off
set PATH=C:\Users\imu\AppData\Local\Microsoft\WinGet\Packages\Ninja-build.Ninja_Microsoft.Winget.Source_8wekyb3d8bbwe;C:\Program Files\LLVM\bin;%PATH%
cd /d C:\Users\imu\Desktop\ImuTechz\IMUCLI~1\imurun\model\fish
if exist build rmdir /s /q build
"C:\Program Files\CMake\bin\cmake.exe" -G Ninja -B build -DCMAKE_BUILD_TYPE=Release -DLLAMA_BUILD_SERVER=ON -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_TOOLS=OFF -DCMAKE_C_COMPILER=clang.exe -DCMAKE_CXX_COMPILER=clang++.exe
if errorlevel 1 exit /b 1
ninja -C build