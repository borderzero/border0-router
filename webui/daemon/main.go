package main

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
)

// Config represents our application configuration.
// Customize this struct with your own configuration settings.
type Config struct {
	SSID string `json:"ssid"`
	PSK  string `json:"psk"`
	// AuthToken string `json:"border0_token,omitempty"`
}

var configFilePath = "./files/config.json"

// Token represents the authentication token structure
type Token struct {
	AuthToken string `json:"border0_token"`
}

var tokenFilePath = "./files/token.json"
var internetAccessFilePath = "./files/internet_access.on"
var defaultHostapdConfigPath = "/opt/border0/templates/hostapd.conf.default"
var templateHostapdConfigPath = "./templates/hostapd.conf"
var hostapdConfigPath = "/etc/hostapd/hostapd.conf"
var hostapdProvisionedPath = "./files/hostapd.provisioned"
var deviceStateFilePath = "/root/.border0/device.state.yaml"

// ExitNodeResponse represents the structure of the response from the exit node list command
type ExitNodeResponse struct {
	ExitNodes []string `json:"exit_nodes"`
}

// NodeStateResponse represents the structure of the response from the state command
type NodeStateResponse struct {
	ExitNode string `json:"exit_node"`
	Peers    []struct {
		Name     string `json:"name"`
		Services []struct {
			Name      string `json:"name"`
			Type      string `json:"type"`
			PublicIPs []struct {
				IPAddress string `json:"ip_address"`
				Type      string `json:"type"`
				Metadata  struct {
					CountryName string  `json:"country_name"`
					CountryCode string  `json:"country_code"`
					RegionName  string  `json:"region_name"`
					CityName    string  `json:"city_name"`
					Latitude    float64 `json:"latitude"`
					Longitude   float64 `json:"longitude"`
					ISP         string  `json:"isp"`
				} `json:"metadata"`
			} `json:"public_ips"`
		} `json:"services"`
	} `json:"peers"`
}

// loadConfig loads the configuration from the config file
func loadConfig() (Config, error) {
	var config Config

	// Read the config file
	data, err := os.ReadFile(configFilePath)
	if err != nil {
		return config, err
	}

	// Parse the JSON data into the Config struct
	err = json.Unmarshal(data, &config)
	if err != nil {
		return config, err
	}

	return config, nil
}

// saveConfig writes the configuration to a JSON file.
func saveConfig(config Config) error {
	file, err := os.Create(configFilePath)
	if err != nil {
		return err
	}
	defer file.Close()

	encoder := json.NewEncoder(file)
	encoder.SetIndent("", "  ") // pretty-print the JSON
	return encoder.Encode(config)
}

// loadToken loads the authentication token from the token file
func loadToken() (Token, error) {
	var token Token

	// Read the token file
	data, err := os.ReadFile(tokenFilePath)
	if err != nil {
		if os.IsNotExist(err) {
			// Return empty token if file doesn't exist yet
			return token, nil
		}
		return token, err
	}

	// Parse the JSON data into the Token struct
	err = json.Unmarshal(data, &token)
	if err != nil {
		return token, err
	}

	return token, nil
}

// saveToken writes the authentication token to a JSON file
func saveToken(token Token) error {
	// Create directory if it doesn't exist
	dir := filepath.Dir(tokenFilePath)
	if err := os.MkdirAll(dir, 0755); err != nil {
		return err
	}

	file, err := os.Create(tokenFilePath)
	if err != nil {
		return err
	}
	defer file.Close()

	encoder := json.NewEncoder(file)
	encoder.SetIndent("", "  ") // pretty-print the JSON
	return encoder.Encode(token)
}

// applyConfigToHostapd applies the configuration to the hostapd.conf file
func applyConfigToHostapd(config Config) error {
	// Read the hostapd template
	template, err := os.ReadFile(templateHostapdConfigPath)
	if err != nil {
		return err
	}

	// Replace variables in the template
	content := string(template)
	content = strings.Replace(content, "${ssid}", config.SSID, -1)
	content = strings.Replace(content, "${psk}", config.PSK, -1)

	// Write to the hostapd configuration file
	err = os.WriteFile(hostapdConfigPath, []byte(content), 0644)
	if err != nil {
		return err
	}

	// Create a privioan crumb by checking if /etc/hostapd/hostapd.provisioned file exists, and create it if it doesn't
	if _, err := os.Stat(hostapdProvisionedPath); os.IsNotExist(err) {
		if err := os.WriteFile(hostapdProvisionedPath, []byte("privioan_crumb"), 0644); err != nil {
			return err
		}
	}

	return nil
}

// rebootSystem executes the reboot command
func rebootSystem() error {
	cmd := exec.Command("reboot")
	return cmd.Run()
}

func rebootWifi() error {
	cmd := exec.Command("reboot")
	return cmd.Run()
}

// Add this function near the top with other helper functions
func isProvisioned() bool {
	_, err := os.Stat(hostapdProvisionedPath)
	return !os.IsNotExist(err)
}

// factoryReset resets the device to factory settings
func factoryReset() error {

	stopBorder0DeviceService()
	disableBorder0DeviceService()

	defaultHostapdConfig, err := os.ReadFile(defaultHostapdConfigPath)
	if err != nil {
		return err
	}

	err = os.WriteFile(hostapdConfigPath, defaultHostapdConfig, 0644)
	if err != nil {
		return err
	}

	if err := os.Remove("/etc/sysconfig/border0-device"); err != nil && !os.IsNotExist(err) {
		return err
	}

	if err := os.Remove(deviceStateFilePath); err != nil && !os.IsNotExist(err) {
		return err
	}

	// Delete config.json
	if err := os.Remove(configFilePath); err != nil && !os.IsNotExist(err) {
		return err
	}

	// Delete token.json
	if err := os.Remove(tokenFilePath); err != nil && !os.IsNotExist(err) {
		return err
	}

	// Delete internet_access.on file
	if err := os.Remove(internetAccessFilePath); err != nil && !os.IsNotExist(err) {
		return err
	}

	// Delete hostapd.provisioned file
	if err := os.Remove(hostapdProvisionedPath); err != nil && !os.IsNotExist(err) {
		return err
	}

	// Restore default hostapd.conf from template
	defaultConfig := Config{
		SSID: "border0",
		PSK:  "border0123",
	}
	if err := saveConfig(defaultConfig); err != nil {
		return err
	}

	return nil
}
func checkAndInsertPostRoutingRules() error {
	cmd := exec.Command("iptables", "-t", "nat", "-L", "POSTROUTING", "-nvx")
	output, err := cmd.Output()
	if err != nil {
		return err
	}

	rules := string(output)
	hasMasqueradeEth0 := strings.Contains(rules, "eth0    192.168.69.0/24      0.0.0.0/0")
	hasMasqueradeUtun9 := strings.Contains(rules, "utun9   192.168.69.0/24      0.0.0.0/0")

	// Check if both rules are present
	if !hasMasqueradeEth0 || !hasMasqueradeUtun9 {
		if !hasMasqueradeEth0 {
			_, err := exec.Command("iptables", "-t", "nat", "-A", "POSTROUTING", "-s", "192.168.69.0/24", "-o", "eth0", "-j", "MASQUERADE").Output()
			if err != nil {
				return err
			}
			log.Printf("Added MASQUERADE rule for eth0")
		}

		if !hasMasqueradeUtun9 {
			_, err := exec.Command("iptables", "-t", "nat", "-A", "POSTROUTING", "-s", "192.168.69.0/24", "-o", "utun9", "-j", "MASQUERADE").Output()
			if err != nil {
				return err
			}
			log.Printf("Added MASQUERADE rule for utun9")
		}
	}

	return nil
}

func enableIPv4Forwarding() error {
	if err := exec.Command("sysctl", "-w", "net.ipv4.ip_forward=1").Run(); err != nil {
		return fmt.Errorf("failed to enable IPv4 forwarding: %v", err)
	}
	log.Println("Enabled IPv4 forwarding")
	return nil
}

func enableBorder0DeviceService() error {
	cmd := exec.Command("systemctl", "enable", "border0-device")
	return cmd.Run()
}

func startBorder0DeviceService() error {
	cmd := exec.Command("systemctl", "restart", "border0-device")
	return cmd.Run()
}

func stopBorder0DeviceService() error {
	cmd := exec.Command("systemctl", "stop", "border0-device")
	return cmd.Run()
}

func disableBorder0DeviceService() error {
	cmd := exec.Command("systemctl", "disable", "border0-device")
	return cmd.Run()
}

func getExitNodes() ([]string, error) {
	cmd := exec.Command("border0", "node", "exitnode", "list", "--json")
	output, err := cmd.Output()
	if err != nil {
		return nil, err
	}

	var response ExitNodeResponse
	if err := json.Unmarshal(output, &response); err != nil {
		return nil, err
	}

	return response.ExitNodes, nil
}

// Add this new function to get the current exit node
func getCurrentExitNode() (string, error) {
	cmd := exec.Command("border0", "node", "exitnode", "show")
	output, err := cmd.Output()
	if err != nil {
		return "", err
	}

	return strings.TrimSpace(string(output)), nil
}

// Replace the getPublicIP function with this new implementation
func getExitNodeInfo() (string, string, error) {
	cmd := exec.Command("border0", "node", "state", "show", "--json")
	output, err := cmd.Output()
	if err != nil {
		return "", "", err
	}

	var state NodeStateResponse
	if err := json.Unmarshal(output, &state); err != nil {
		return "", "", err
	}

	// Find the current exit node's public IP and location
	currentExitNode := state.ExitNode
	publicIP := ""
	location := ""

	for _, peer := range state.Peers {
		for _, service := range peer.Services {
			if service.Type == "exit_node" && peer.Name == currentExitNode && len(service.PublicIPs) > 0 {
				publicIP = service.PublicIPs[0].IPAddress
				metadata := service.PublicIPs[0].Metadata
				if metadata.CityName != "" && metadata.CountryName != "" {
					location = fmt.Sprintf("%s, %s", metadata.CityName, metadata.CountryName)
				} else if metadata.CountryName != "" {
					location = metadata.CountryName
				}
				break
			}
		}
	}

	return publicIP, location, nil
}

func main() {

	// Check if IPv4 forwarding is enabled
	checkAndInsertPostRoutingRules()
	enableIPv4Forwarding()

	// Create a Gin router with default middleware (logger, recovery)
	router := gin.Default()

	// Serve static files
	router.Static("/static", "./static")

	// Tell Gin where our HTML templates are located
	router.LoadHTMLGlob("templates/*")

	// GET endpoint for the login page
	router.GET("/login", func(c *gin.Context) {

		host := c.Request.Host
		isGatewayHost := strings.HasPrefix(host, "gateway.border0")

		data := gin.H{
			"provisioned":   isProvisioned(),
			"isGatewayHost": isGatewayHost,
		}

		c.HTML(http.StatusOK, "login.html", data)
	})

	// GET endpoint for the root path to load home.html
	router.GET("/", func(c *gin.Context) {
		host := c.Request.Host
		isGatewayHost := strings.HasPrefix(host, "gateway.border0")

		// Check if device state file exists
		deviceStateExists := false
		if _, err := os.Stat(deviceStateFilePath); err == nil {
			deviceStateExists = true
		}

		exitNodes, _ := getExitNodes()             // Get exit nodes list
		currentExitNode, _ := getCurrentExitNode() // Get current exit node

		// Get public IP and location from state command
		publicIP, location, err := getExitNodeInfo()
		if err != nil || publicIP == "" {
			publicIP = "unavailable"
			location = "unavailable"
		}

		c.HTML(http.StatusOK, "home.html", gin.H{
			"provisioned":       isProvisioned(),
			"isGatewayHost":     isGatewayHost,
			"deviceStateExists": deviceStateExists,
			"exitNodes":         exitNodes,
			"currentExitNode":   currentExitNode,
			"publicIP":          publicIP,
			"location":          location,
		})
	})

	// GET endpoint for the configuration page
	router.GET("/config", func(c *gin.Context) {
		config, err := loadConfig()
		data := gin.H{
			"provisioned": isProvisioned(),
			"config":      config,
		}
		if err != nil {
			data["error"] = "Error loading configuration: " + err.Error()
			data["config"] = Config{} // Provide empty config to avoid template errors
		}
		c.HTML(http.StatusOK, "config.html", data)
	})

	// GET endpoint for system status
	router.GET("/status", func(c *gin.Context) {
		data := gin.H{
			"provisioned": isProvisioned(),
		}
		var errorMsg string

		// Get status
		status, err := exec.Command("border0", "node", "debug", "peers").Output()
		if err != nil {
			errorMsg += "Error getting status: " + err.Error() + "\n"
		} else {
			data["status"] = strings.TrimSpace(string(status))
		}

		if errorMsg != "" {
			data["error"] = errorMsg
		}

		c.HTML(http.StatusOK, "status.html", data)
	})

	// POST endpoint for updating the configuration file
	router.POST("/config", func(c *gin.Context) {
		// Retrieve form values from the POST request
		ssid := c.PostForm("ssid")
		psk := c.PostForm("psk")

		// Validate PSK length
		if len(psk) < 8 {
			c.HTML(http.StatusBadRequest, "config.html", gin.H{
				"config": Config{SSID: ssid, PSK: psk},
				"error":  "PSK must be at least 8 characters long",
			})
			return
		}

		// Create a new Config object with updated values
		newConfig := Config{
			SSID: ssid,
			PSK:  psk,
		}

		// Write the updated config back to the file
		if err := saveConfig(newConfig); err != nil {
			c.String(http.StatusInternalServerError, "Error saving config: %v", err)
			return
		}

		// Redirect back to the config page after a successful update
		c.Redirect(http.StatusFound, "/config")
	})

	// POST endpoint for factory reset
	router.POST("/factory_reset", func(c *gin.Context) {
		if err := factoryReset(); err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": "Failed to perform factory reset: " + err.Error()})
			return
		}

		// Trigger system reboot
		go func() {
			// Wait a moment to allow the response to be sent
			time.Sleep(1 * time.Second)
			if err := rebootSystem(); err != nil {
				log.Printf("Failed to reboot: %v", err)
			}
		}()

		c.JSON(http.StatusOK, gin.H{"message": "Factory reset successful, system will reboot"})
	})

	// Add these new endpoints
	router.GET("/api/exitnodes", func(c *gin.Context) {
		nodes, err := getExitNodes()
		if err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
			return
		}
		c.JSON(http.StatusOK, gin.H{"exit_nodes": nodes})
	})

	router.POST("/api/exitnode", func(c *gin.Context) {
		node := c.PostForm("node")
		cmd := exec.Command("border0", "node", "exitnode", "set", node)
		if err := cmd.Run(); err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
			return
		}
		c.JSON(http.StatusOK, gin.H{"message": "Exit node set successfully"})
	})

	// Add new endpoint for unsetting exit node
	router.POST("/api/exitnode/unset", func(c *gin.Context) {
		cmd := exec.Command("border0", "node", "exitnode", "unset", "--json")
		if err := cmd.Run(); err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
			return
		}
		c.JSON(http.StatusOK, gin.H{"message": "Exit node unset successfully"})
	})

	// Start the HTTP server on multiple ports
	go func() {
		if err := router.Run(":443"); err != nil {
			log.Printf("Failed to start server on port 443: %v", err)
		}
	}()

	go func() {
		if err := router.Run(":80"); err != nil {
			log.Printf("Failed to start server on port 80: %v", err)
		}
	}()

	// Run the main server on port 8080 (this will block)
	if err := router.Run(":8080"); err != nil {
		log.Fatalf("Failed to start server on port 8080: %v", err)
	}
}
