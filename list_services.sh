#!/bin/bash

# 1. Capture all listening PIDs in your target port range
pids=$(sudo lsof -i tcp:11400-11420 -s tcp:LISTEN -t 2>/dev/null | sort -u)

echo -e "PORT\tPID\tPROCESS TYPE"
echo -e "--------------------------------------------------------------------------------"

for pid in $pids; do
    # Get the listening port
    port=$(sudo lsof -i tcp:11400-11420 -s tcp:LISTEN -a -p $pid -F n 2>/dev/null | grep -o ':[0-9]*$' | tr -d ':' | head -n1)
    
    # Read the command line string from /proc
    cmdline=$(tr '\0' ' ' < /proc/$pid/cmdline 2>/dev/null)
    
    # Track down the working directory
    cwd=$(sudo readlink /proc/$pid/cwd 2>/dev/null)
    
    # Read the clean process binary name
    proc_name=$(ps -p $pid -o comm= 2>/dev/null)
    
    # Extract the actual systemd service file name via the process cgroup string
    svc_file=$(cat /proc/$pid/cgroup 2>/dev/null | grep -oP '(?<=/user@1000.service/).*?\.(service|scope|slice)' | head -n1 | sed 's|.*/||')
    
    # Check if the process is under a specific app slice or scope inside user manager
    if [ -z "$svc_file" ]; then
        svc_file=$(cat /proc/$pid/cgroup 2>/dev/null | grep -oP '(?<=/).*?\.service' | head -n1 | sed 's|.*/||')
    fi

    # Final fallback if it completely bypasses the systemd ecosystem
    if [ -z "$svc_file" ]; then
        svc_file="None (Manual Process)"
    fi
    
    echo -e "[$port]\t$pid\t$proc_name"
    echo -e "\t\tID:  $svc_file"
    echo -e "\t\tDir: $cwd"
    echo -e "\t\tCmd: $cmdline"
    echo -e "--------------------------------------------------------------------------------"
done
