{{- define "ip.labels" -}}
app.kubernetes.io/part-of: incidentpilot
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "ip.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag }}
{{- end -}}

{{- define "ip.ollamaUrl" -}}
http://{{ .Release.Name }}-ollama:11434
{{- end -}}
