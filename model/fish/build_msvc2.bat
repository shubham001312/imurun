@echo off
call "C:\Program Files\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat" -arch=amd64 -host_arch=amd64
cd /d C:\Users\imu\Desktop\ImuTechz\IMUCLI~1\imurun\model\fish
if exist build rmdir /s /q build
"C:\Program Files\CMake\bin\cmake.exe" -G Ninja -B build -DCMAKE_BUILD_TYPE=Release -DLLAMA_BUILD_SERVER=ON -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_TOOLS=OFF