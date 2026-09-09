param(
  [int]$Port = 5000,
  [int]$Threads = 8
)

$env:PORT = $Port
$env:WAITRESS_THREADS = $Threads
python -m wsgi
