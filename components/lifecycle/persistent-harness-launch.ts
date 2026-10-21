import {spawn} from 'node:child_process';
import {resolve} from 'node:path';

export function persistentWindowsShell(){return resolve(process.env.SystemRoot??process.env.SYSTEMROOT??'C:/Windows','System32/WindowsPowerShell/v1.0/powershell.exe');}
// Manual operator start only. Node detached=true does not leave the execution
// Job chain. The documented Windows process broker gives the guardian its own
// lifetime; its workplace Job still contains the complete backend/SSH tree.
const script=String.raw`
$ErrorActionPreference='Stop'
[Console]::InputEncoding=[System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)
$p=[Console]::In.ReadToEnd() | ConvertFrom-Json
if($p.schema -ne 'persistent-manual-launch/v1'){throw 'invalid_manual_launch'}
foreach($f in @($p.python,$p.guardian,$p.node,$p.hostEntry,$p.hostPath,$p.identity)){
 if(-not [IO.Path]::IsPathRooted($f) -or -not (Test-Path -LiteralPath $f -PathType Leaf) -or $f.Contains('"') -or $f.Contains([char]10) -or $f.Contains([char]13)){throw 'invalid_manual_launch_path'}
}
$h=Get-Content -LiteralPath $p.hostPath -Raw -Encoding UTF8 | ConvertFrom-Json
if([IO.Path]::GetFullPath($h.owner.workplaceIdentityPath) -ne [IO.Path]::GetFullPath($p.identity)){throw 'manual_launch_identity_mismatch'}
if([IO.Path]::GetFileName($p.guardian) -ne 'workplace_owner.py' -or [IO.Path]::GetFileName($p.hostEntry) -ne 'persistent-harness-host-cli.mjs' -or [IO.Path]::GetFileName($p.node) -ne 'node.exe' -or [double]$p.cooldown -lt 0 -or [double]$p.cooldown -gt 86400){throw 'invalid_manual_launch_entry'}
$environment=@();foreach($v in $p.environment.PSObject.Properties){if($v.Name -eq 'SOLUTION_AGENT_OWNER_LEASE'){throw 'manual_launch_inherited_lease_rejected'};$environment+=($v.Name+'='+[string]$v.Value)}
$startup=New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property @{ShowWindow=[uint16]0;EnvironmentVariables=[string[]]$environment}
$argv=@($p.python,$p.guardian,'--identity',$p.identity,'--cooldown-seconds',[string]$p.cooldown,'--log-file',($p.hostPath+'.log'),'--',$p.node,$p.hostEntry,'host',$p.hostPath)
$command=($argv | ForEach-Object {'"'+$_+'"'}) -join ' '
$r=Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{CommandLine=$command;CurrentDirectory=[IO.Path]::GetDirectoryName($p.hostEntry);ProcessStartupInformation=$startup}
if($r.ReturnValue -ne 0){throw ('manual_launch_broker_failed_'+$r.ReturnValue)}
[Console]::Out.Write((@{manualLaunch=$true;brokerPid=$r.ProcessId;automaticRetries=0}|ConvertTo-Json -Compress))
`;

export async function launchPersistentWindows(input:{python:string,guardian:string,node:string,hostEntry:string,hostPath:string,identity:string,cooldown:number},env:NodeJS.ProcessEnv){
 if(process.platform!=='win32')throw Error('persistent_windows_launch_only');
 const environment={...env};delete environment.SOLUTION_AGENT_OWNER_LEASE;
 const payload=JSON.stringify({schema:'persistent-manual-launch/v1',...input,environment});
 return new Promise<{manualLaunch:boolean,brokerPid:number,automaticRetries:number}>((yes,no)=>{
  const child=spawn(persistentWindowsShell(),['-NoLogo','-NoProfile','-NonInteractive','-Command',script],{env,windowsHide:true,stdio:['pipe','pipe','pipe']});
  let out='',err='';let expired=false;
  const timer=setTimeout(()=>{expired=true;child.kill();},15000);
  child.stdout.on('data',b=>{out+=b;});child.stderr.on('data',b=>{err+=b;});
  child.once('error',e=>{clearTimeout(timer);no(e);});
  child.once('close',code=>{clearTimeout(timer);if(expired)return no(Error('persistent_manual_broker_timeout_no_retry'));if(code!==0)return no(Error('persistent_manual_broker_failed_no_retry: '+err.slice(-500)));try{yes(JSON.parse(out));}catch{no(Error('persistent_manual_broker_invalid_response_no_retry'));}});
  child.stdin.on('error',()=>{});child.stdin.end(payload);
 });
}
