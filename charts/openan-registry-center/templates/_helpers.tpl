{{- define "rc.componentName" -}}{{ .Release.Name }}-{{ .Values.component.name }}{{- end -}}
{{- define "rc.fullname" -}}{{ .Release.Name }}-registry-center{{- end -}}
{{- define "rc.labels" -}}
app: {{ include "rc.fullname" . }}
oda.tmforum.org/componentName: {{ include "rc.componentName" . }}
app.kubernetes.io/name: openan-registry-center
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
