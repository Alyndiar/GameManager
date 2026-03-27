param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $PytestArgs
)

$ErrorActionPreference = "Stop"

$cmd = @("-n", "GameManager", "python", "-m", "pytest")
if ($PytestArgs) {
    $cmd += $PytestArgs
}

conda run @cmd
exit $LASTEXITCODE

