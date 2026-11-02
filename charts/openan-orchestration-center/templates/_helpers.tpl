{{- define "oc.componentName" -}}{{ .Release.Name }}-{{ .Values.component.name }}{{- end -}}
{{- define "oc.fullname" -}}{{ .Release.Name }}-orchestration-center{{- end -}}
{{- define "oc.labels" -}}
app: {{ include "oc.fullname" . }}
oda.tmforum.org/componentName: {{ include "oc.componentName" . }}
app.kubernetes.io/name: openan-orchestration-center
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
