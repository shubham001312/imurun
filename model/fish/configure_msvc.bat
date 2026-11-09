@echo off
call "C:\Program Files\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat" -arch=amd64 -host_arch=amd64
cmake -G Ninja -B "C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\model\fish\build" -DCMAKE_BUILD_TYPE=Release -DLLAMA_BUILD_SERVER=ON -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_TOOLS=OFF -S "C:\Users\imu\Desktop\ImuTechz\IMU CLI\imurun\model\fish"