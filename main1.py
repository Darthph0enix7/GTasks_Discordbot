import subprocess
import schedule
import time
import psutil

# Function to start and manage the processes
def run_scripts():
    # Stop any previous instances of main1.py and main2.py
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        if proc.info['cmdline'] and ('main1.py' in proc.info['cmdline'] or 'main2.py' in proc.info['cmdline']):
            print(f"Terminating process {proc.info['pid']} ({proc.info['name']})")
            proc.terminate()
            proc.wait()

    # Start main1.py and main2.py as separate processes
    process1 = subprocess.Popen(['python', 'main1.py'])
    process2 = subprocess.Popen(['python', 'main2.py'])

    # Wait for both processes to complete
    process1.wait()
    process2.wait()

    print("Both scripts have finished executing.")

# Schedule the function to run every hour
schedule.every().hour.do(run_scripts)

# Run the function once at the start
run_scripts()

# Keep the script running to maintain the schedule
while True:
    schedule.run_pending()
    time.sleep(1)