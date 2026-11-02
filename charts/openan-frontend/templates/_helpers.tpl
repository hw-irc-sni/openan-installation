{{- define "fe.componentName" -}}{{ .Release.Name }}-{{ .Values.component.name }}{{- end -}}
{{- define "fe.fullname" -}}{{ .Release.Name }}-frontend{{- end -}}
{{- define "fe.labels" -}}
app: {{ include "fe.fullname" . }}
oda.tmforum.org/componentName: {{ include "fe.componentName" . }}
app.kubernetes.io/name: openan-frontend
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
