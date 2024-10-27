import subprocess

# Start main1.py and main2.py as separate processes
process1 = subprocess.Popen(['python', 'main1.py'])
process2 = subprocess.Popen(['python', 'main2.py'])

# Wait for both processes to complete
process1.wait()
process2.wait()

print("Both scripts have finished executing.")