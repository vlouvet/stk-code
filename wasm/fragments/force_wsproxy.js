/* REPLACE
if\s*\(SOCKFS\.websocketArgs\[['"]url['"]\]\)\s*\{\s*url\s*=\s*SOCKFS\.websocketArgs\[['"]url['"]\];?\s*\}
*/
if(SOCKFS.websocketArgs["url"]){
  url=SOCKFS.websocketArgs["url"];
  if(!url.endsWith("/"))url+="/";
  if(sock.type===2/*SOCK_DGRAM*/)url+="udp/";
  var parts=addr.split("/");
  url+=parts[0]+":"+port;
}

/* INSERT
var ?opts ?= ?undefined;
*/
var parts = addr.split("/");
if (!url.endsWith("/")) url += "/";
if (sock.type === 2 /* SOCK_DGRAM */) url += "udp/";
url += parts[0] + ":" + port;
