{{- define "openan-operator.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "openan-operator.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "openan-operator.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "openan-operator.selectorLabels" -}}
app.kubernetes.io/name: {{ include "openan-operator.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "openan-operator.labels" -}}
helm.sh/chart: {{ include "openan-operator.chart" . }}
{{ include "openan-operator.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "openan-operator.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "openan-operator.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/* Registry prefix from global.image.registry (empty when unset) */}}
{{- define "docker.registry" -}}
{{- if .Values.global -}}{{- if .Values.global.image -}}{{- if .Values.global.image.registry -}}
{{ .Values.global.image.registry }}
{{- end -}}{{- end -}}{{- end -}}
{{- end -}}

{{/* image + version + prereleaseSuffix */}}
{{- define "openan-operator.dockerImage" -}}
{{ include "docker.registry" . }}{{ .Values.deployment.image }}:{{ .Values.deployment.version }}
{{- if .Values.deployment.prereleaseSuffix -}}-{{ .Values.deployment.prereleaseSuffix }}{{- end -}}
{{- end -}}

{{/* Always pull prerelease images */}}
{{- define "openan-operator.imagePullPolicy" -}}
{{- if .Values.deployment.prereleaseSuffix -}}Always{{- else -}}{{ .Values.deployment.imagePullPolicy }}{{- end -}}
{{- end -}}

{{/* "ns1,ns2" -> "-n ns1 -n ns2" */}}
{{- define "openan-operator.monitoredNamespacesCLIOpts" -}}
{{- printf "-n %s" .Values.deployment.monitoredNamespaces | replace "," " -n " }}
{{- end -}}
